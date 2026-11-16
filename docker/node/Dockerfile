# Imagem-base única para KMEs e APPs (SAEs). O papel é escolhido por ROLE no entrypoint.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends iproute2 iputils-ping iperf3 tcpdump curl procps \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY kme/ /opt/qnetlab/kme/
COPY sae/ /opt/qnetlab/sae/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
