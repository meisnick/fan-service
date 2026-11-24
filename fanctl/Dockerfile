FROM debian:bookworm-slim
# debian (glibc) so the nvidia-smi injected by the NVIDIA runtime (NVIDIA_DRIVER_CAPABILITIES=utility) can run
RUN apt-get update && apt-get install -y --no-install-recommends python3 ipmitool smartmontools sg3-utils && rm -rf /var/lib/apt/lists/*
# code is bind-mounted read-only from /mnt/apps/fanctl/app (update = copy files + restart the app)
CMD ["python3", "-u", "/app/fanctl.py", "--data", "/config", "--logs", "/logs", "--port", "8090"]
