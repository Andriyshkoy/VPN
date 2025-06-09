FROM andriyshkoy/vpn-base:latest

COPY alembic ./alembic
COPY alembic.ini .

ENTRYPOINT ["alembic", "upgrade", "head"]
