FROM andriyshkoy/vpn-base:latest

COPY admin ./admin

CMD ["uvicorn", "admin.app:app", "--host=0.0.0.0", "--port=8000"]
