# Gerado por QNetLab — camada específica do nó sobre a imagem-base.
FROM qnetlab/node:base
COPY certs/ /certs/
# Configuração deste KME (esquema do KMS-sim), gerada a partir do plano da topologia
COPY kme_config.yaml /etc/qnetlab/kme_config.yaml
RUN mkdir -p /opt/kme/data && chmod 600 /etc/qnetlab/kme_config.yaml
# main.py do KME vindo de containers.kme_source_dir (substitui o vendorizado)
COPY kme_src/ /opt/qnetlab/kme/
RUN chmod 700 /certs && chmod 600 /certs/*.key.pem
