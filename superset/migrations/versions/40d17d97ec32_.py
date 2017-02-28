"""empty message

Revision ID: 40d17d97ec32
Revises: d6db5a5cdb5d
Create Date: 2017-02-28 14:46:17.556261

"""

# revision identifiers, used by Alembic.
revision = '40d17d97ec32'
down_revision = 'd6db5a5cdb5d'

from alembic import op
import json
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import Column, Integer, String, Text

from superset import db
from superset.legacy import cast_form_data

Base = declarative_base()


class Slice(Base):
    """Declarative class to do query in upgrade"""
    __tablename__ = 'slices'
    id = Column(Integer, primary_key=True)
    datasource_type = Column(String(200))
    slice_name = Column(String(200))
    params = Column(Text)

def cast_back_filters(form_data):
  """Cast array back to string for regex and equal filters"""
  flts = []
  having_flts = []
  fd = form_data
  for filter_type in ['filters', 'having_filters']:
    for flt in fd[filter_type]:
      if flt['op'] not in ['in', 'not in']:
        flt['val'] = len(flt['val']) > 0 ? flt['val'][0] : ''
        if filter_type == 'filters':
          flts.append(flt)
        else:
          having_flts.append(flt)
  fd['filters'] = flts
  fd['having_filters'] = having_flts
  return fd

def upgrade():
    bind = op.get_bind()
    session = db.Session(bind=bind)

    slices = session.query(Slice).all()
    slice_len = len(slices)
    for i, slc in enumerate(slices):
        try:
            d = json.loads(slc.params or '{}')
            d = cast_back_filters(d)
            slc.params = json.dumps(d, indent=2, sort_keys=True)
            session.merge(slc)
            session.commit()
            print('Upgraded ({}/{}): {}'.format(i, slice_len, slc.slice_name))
        except Exception as e:
            print(slc.slice_name + ' error: ' + str(e))

    session.close()


def downgrade():
    pass
