# QNetLab bootstrap (console serial) — MKT21
# Objetivo: IP de gerenciamento (NAT do GNS3) + REST (www) + FTP + senha do admin. Idempotente.
/ip dhcp-client remove [find]
/ip address remove [find where comment="mgmt-bootstrap"]
/ip address add address=192.168.122.30/24 interface=ether8 comment=mgmt-bootstrap
/ip service set www disabled=no address=192.168.122.0/24
/ip service set ftp disabled=no address=192.168.122.0/24
/user set [find where name="admin"] password="qnetlab-lab"
