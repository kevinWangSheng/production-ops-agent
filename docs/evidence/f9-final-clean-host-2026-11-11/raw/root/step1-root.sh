set -ex
date -u +%FT%TZ
adduser --disabled-password --gecos "" opspilot
su - opspilot -c "git clone https://github.com/kevinWangSheng/production-ops-agent.git"
su - opspilot -c "cd production-ops-agent && git log --oneline -1"
/home/opspilot/production-ops-agent/scripts/kind_lab/install_linux.sh --user opspilot
date -u +%FT%TZ
