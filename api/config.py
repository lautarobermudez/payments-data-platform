"""Settings, read from the environment (nothing is hard-coded except the local demo default)."""
import os

# The role api_reader is created by db/06_api_role.sql. These are local demo credentials.
DEFAULT_DATABASE_URL = 'postgresql://api_reader:api_reader@localhost:5432/payments'


def database_url() -> str:
    return os.environ.get('API_DATABASE_URL', DEFAULT_DATABASE_URL)
