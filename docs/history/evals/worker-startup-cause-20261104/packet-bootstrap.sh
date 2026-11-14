set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends curl tcpdump tshark ca-certificates > /tmp/install.log 2>&1
python3 /tmp/probe.py
