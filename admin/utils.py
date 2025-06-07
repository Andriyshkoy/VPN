from dataclasses import asdict
from datetime import datetime
from decimal import Decimal


def serialize_dataclass(obj):
    """Convert dataclass instance into JSON serializable ``dict``."""
    data = asdict(obj)
    for key, value in data.items():
        if isinstance(value, datetime):
            data[key] = value.isoformat()
        elif isinstance(value, Decimal):
            data[key] = float(value)
    return data
