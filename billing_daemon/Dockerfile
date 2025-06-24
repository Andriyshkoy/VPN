FROM andriyshkoy/vpn-base:latest

COPY billing_daemon ./billing_daemon

ENTRYPOINT ["/bin/sh", "-c"]
CMD ["python", "-m", "billing_daemon.main"]
