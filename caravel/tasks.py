import celery
from caravel import models, app, utils
from datetime import datetime
import logging
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.sql.expression import TextAsFrom
import sqlparse
import pandas as pd

celery_app = celery.Celery(config_source=app.config.get('CELERY_CONFIG'))


def is_query_select(sql):
    try:
        return sqlparse.parse(sql)[0].get_type() == 'SELECT'
    # Capture sqlparse exceptions, worker shouldn't fail here.
    except Exception:
        # TODO(bkyryliuk): add logging here.
        return False


# if sqlparse provides the stream of tokens but don't provide the API
# to access the table names, more on it:
# https://groups.google.com/forum/#!topic/sqlparse/sL2aAi6dSJU
# https://github.com/andialbrecht/sqlparse/blob/master/examples/
# extract_table_names.py
#
# Another approach would be to run the EXPLAIN on the sql statement:
# https://prestodb.io/docs/current/sql/explain.html
# https://cwiki.apache.org/confluence/display/Hive/LanguageManual+Explain
def get_tables():
    """Retrieves the query names from the query."""
    # TODO(bkyryliuk): implement parsing the sql statement.
    pass


def add_limit_to_the_sql(sql, limit, eng):
    # Treat as single sql statement in case of failure.
    sql_statements = [sql]
    try:
        sql_statements = [s for s in sqlparse.split(sql) if s]
    except Exception as e:
        logging.info(
            "Statement " + sql + "failed to be transformed to have the limit "
            "with the exception" + e.message)
        return sql
    if len(sql_statements) == 1 and is_query_select(sql):
        qry = select('*').select_from(
            TextAsFrom(text(sql_statements[0]), ['*']).alias(
                'inner_qry')).limit(limit)
        sql_statement = str(qry.compile(
            eng, compile_kwargs={"literal_binds": True}))
        return sql_statement
    return sql


# create table works only for the single statement.
def create_table_as(sql, table_name, override=False):
    """Reformats the query into the create table as query.

    Works only for the single select SQL statements, in all other cases
    the sql query is not modified.
    :param sql: string, sql query that will be executed
    :param table_name: string, will contain the results of the query execution
    :param override, boolean, table table_name will be dropped if true
    :return: string, create table as query
    """
    # TODO(bkyryliuk): drop table if allowed, check the namespace and
    #                  the permissions.
    # Treat as single sql statement in case of failure.
    sql_statements = [sql]
    try:
        # Filter out empty statements.
        sql_statements = [s for s in sqlparse.split(sql) if s]
    except Exception as e:
        logging.info(
            "Statement " + sql + "failed to be transformed as create table as "
            "with the exception" + e.message)
        return sql
    if len(sql_statements) == 1 and is_query_select(sql):
        updated_sql = ''
        # TODO(bkyryliuk): use sqlalchemy statements for the
        #  the drop and create operations.
        if override:
            updated_sql = 'DROP TABLE IF EXISTS {};\n'.format(table_name)
        updated_sql += "CREATE TABLE %s AS %s" % (
            table_name, sql_statements[0])
        return updated_sql
    return sql


def get_session():
    """Creates new SQLAlchemy scoped_session."""
    engine = create_engine(
        app.config.get('SQLALCHEMY_DATABASE_URI'), convert_unicode=True)
    return scoped_session(sessionmaker(
        autocommit=False, autoflush=False, bind=engine))


@celery_app.task
def get_sql_results(query_id):
    """Executes the sql query returns the results.

    :param database_id: integer
    :param sql: string, query that will be executed
    :param user_id: integer
    :param tmp_table_name: name of the table for CTA
    :param schema: string, name of the schema (used in presto)
    :return: dataframe, query result
    """
    # Create a separate session, reusing the db.session leads to the
    # concurrency issues.
    session = get_session()
    query = session.query(models.Query).filter_by(id=query_id).first()
    try:
        db_to_query = (
            session.query(models.Database).filter_by(id=query.database_id)
            .first()
        )
    except Exception as e:
        query.status = models.QueryStatus.FAILED
        query.error_message = utils.error_msg_from_exception(e)
        session.commit()
        return {
            'error': query.error_message,
            'status': query.status,
        }
    if not db_to_query:
        query.status = models.QueryStatus.FAILED
        query.error_message = "Database with id {0} is missing.".format(
            query.database_id)
        session.commit()
        return {
            'error': query.error_message,
            'status': query.status,
        }

    # TODO(bkyryliuk): run explain query to derive the tables and fill in the
    #                  table_ids
    # TODO(bkyryliuk): check the user permissions
    if not query.tmp_table_name:
        query.tmp_table_name = 'tmp_{}_table_{}'.format(
            query.user_id, query.start_time.strftime('%Y_%m_%d_%H_%M_%S'))

    query_result = get_sql_results_as_dict(db_to_query, query)
    query.end_time = datetime.now()
    session.commit()
    # TODO(bkyryliuk): return the tmp table  / query_id
    return query_result


def get_sql_results_as_dict(db_to_query, query):
    """Get the SQL query results from the give session and db connection.

    :param sql: string, query that will be executed
    :param db_to_query: models.Database to query, cannot be None
    :param query: models.Query, has to have tmp_table_name and query_id
    :param schema: string, name of the schema (used in presto)
    :return: (dataframe, boolean), results and the status
    """
    eng = db_to_query.get_sqla_engine(schema=query.schema)
    sql = query.sql.strip().strip(';')
    # TODO(bkyryliuk): fix this case for multiple statements
    if query.limit:
        sql = add_limit_to_the_sql(sql, query.limit, eng)

    cta_used = False
    if query.select_as_cta and is_query_select(sql):
        sql = create_table_as(sql, query.tmp_table_name)
        cta_used = True

    print("LOGGGGGG")
    print(str(query.name))
    print(str(query.sql))
    print(str(query.select_as_cta))
    print(str(db_to_query.select_as_create_table_as))
    if cta_used:
        try:
            # TODO(bkyryluk) async update the query status
            eng.execute(sql)
            query.status = models.QueryStatus.FINISHED
            return {
                'query_id': query.id,
                'status': query.status,
            }
        except Exception as e:
            query.error_message = utils.error_msg_from_exception(e)
            query.status = models.QueryStatus.FAILED
            return {
                'error': query.error_message,
                'status': query.status,
            }

    # otherwise run regular SQL query.
    # TODO(bkyryliuk): rewrite into eng.execute as queries different from
    #                  select should be permitted too.
    try:
        df = db_to_query.get_df(sql, query.schema)
        df = df.fillna(0)
        query.status = models.QueryStatus.FINISHED
        return {
            'columns': [c for c in df.columns],
            'data': df.to_dict(orient='records'),
            'status': query.status,
        }
    except Exception as e:
        query.error_message = utils.error_msg_from_exception(e)
        query.status = models.QueryStatus.FAILED
        return {
            'error': query.error_message,
            'status': models.QueryStatus.FAILED,
        }


