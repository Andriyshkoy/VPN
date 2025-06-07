from datetime import datetime
from decimal import Decimal

from admin.utils import serialize_dataclass
from core.services.models import User, Server


def test_serialize_dataclass_handles_datetime_and_decimal():
    user = User(id=1, tg_id=2, username='name', created=datetime(2025, 1, 1, 12, 0), balance=1.5)
    server = Server(id=1, name='srv', ip='1.1.1.1', port=22, host='host', monthly_cost=Decimal('3.5'), location='US', api_key='key')

    data_user = serialize_dataclass(user)
    data_server = serialize_dataclass(server)

    assert data_user['created'] == user.created.isoformat()
    assert isinstance(data_server['monthly_cost'], float) and data_server['monthly_cost'] == float(server.monthly_cost)
