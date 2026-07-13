from dataclasses import asdict
from datetime import datetime
from decimal import Decimal


def serialize_dataclass(obj):
    """Convert dataclass instance into JSON serializable ``dict``."""
    data = asdict(obj)
    for key, value in data.items():
        if key == "api_key":
            # Keep the response shape stable for the existing frontend while
            # preventing decrypted VPN Manager credentials from reaching it.
            data[key] = "********"
        elif isinstance(value, datetime):
            data[key] = value.isoformat()
        elif isinstance(value, Decimal):
            data[key] = float(value)
    return data
