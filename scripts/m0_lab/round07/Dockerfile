FROM python:3.12.12-slim

WORKDIR /runner
COPY scripts/m0_lab/round07 /runner/scripts/m0_lab/round07
COPY scripts/m0_environment/report_contract.py /runner/scripts/m0_environment/report_contract.py
COPY docs/evidence/m0-real-environment/ModelReport.v1.schema.json /runner/docs/evidence/m0-real-environment/ModelReport.v1.schema.json
COPY docs/evidence/m0-real-investigation/round-07-upstream-runs/packet-m004-normal.json /runner/packets/packet-m004-normal.json
COPY docs/evidence/m0-real-investigation/round-07-upstream-runs/packet-m004-fault.json /runner/packets/packet-m004-fault.json

ENV PYTHONPATH=/runner
ENTRYPOINT ["python", "-m", "scripts.m0_lab.round07.candidate_runner"]
