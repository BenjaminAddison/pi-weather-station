# Builder image for the 'weatherpi-builder' container (optional, see docs/image-build.md).
# Bakes in the tools build.sh needs so they survive 'docker compose down'.
#
# To use: copy this file next to the builder's docker-compose.yml and in it
# replace   image: debian:trixie   with   build: .
# then: docker compose build && docker compose up -d
FROM debian:trixie
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      xz-utils qemu-user-static parted fdisk e2fsprogs dosfstools mtools rsync file \
      sqlite3 python3 zerofree curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /work
CMD ["sleep", "infinity"]
