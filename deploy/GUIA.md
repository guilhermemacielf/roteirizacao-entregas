# Deploy no VPS (Hetzner Cloud)

Guia pra subir a roteirização num servidor que roda 24/7, acessível de
qualquer lugar. Flask + OSRM num único servidor via Docker Compose.

## Resumo da arquitetura

```
Internet → :80 → container app (Flask/gunicorn) → container osrm (rede interna)
                       ↓
              volume ./dados (cache geocode, config, valores, token OAuth)
              ./oauth_client.json (secret, read-only)
```

OSRM NÃO é exposto pra internet — só o Flask na porta 80.

---

## Fase 1 — Criar o servidor (Hetzner Cloud)

1. Cria conta em https://console.hetzner.cloud (cartão ou PayPal)
2. **+ NEW PROJECT** → nome "roteirizacao"
3. **+ ADD SERVER**:
   - Location: **Falkenstein** ou **Ashburn** (US, menor latência pro Brasil é Ashburn)
   - Image: **Ubuntu 24.04**
   - Type: **CX22** (2 vCPU, 4GB RAM, 40GB) — €4/mês. Suficiente.
   - SSH Key: cola sua chave pública (ou cria senha — menos seguro)
   - Nome: `rotas`
   - **CREATE & BUY NOW**
4. Anota o **IP público** do servidor.

## Fase 2 — Colocar o código no servidor

**Opção A — GitHub (recomendado, facilita atualizar depois):**
```bash
# No seu PC, uma vez: criar repo privado e push (precisa do gh CLI ou web)
# Depois, no servidor:
ssh root@SEU_IP
git clone https://github.com/SEU_USUARIO/roteirizacao-entregas.git /opt/roteirizacao
```

**Opção B — cópia direta (scp), sem GitHub:**
```bash
# No seu PC (PowerShell), manda a pasta (exceto dados pesados):
scp -r C:\Users\guilh\Documents\projetos\roteirizacao-entregas root@SEU_IP:/opt/roteirizacao
```

## Fase 3 — Setup automático

```bash
ssh root@SEU_IP
cd /opt/roteirizacao
bash deploy/setup-vps.sh
```
Isso instala Docker, baixa+processa o mapa RMBH (~10-15min), configura firewall.

## Fase 4 — Secrets e subir

```bash
cd /opt/roteirizacao

# 1. Copia o oauth_client.json (do seu PC via scp, ou cria novo no GCP)
#    scp C:\...\oauth_client.json root@SEU_IP:/opt/roteirizacao/

# 2. Cria .env com a chave do Google Maps
echo 'GOOGLE_MAPS_API_KEY=AIza...SUA_KEY' > .env

# 3. (opcional) Copia o cache de geocode pré-populado do seu PC:
#    scp C:\...\dados\geocode.cache.json root@SEU_IP:/opt/roteirizacao/dados/

# 4. Sobe tudo
docker compose -f docker-compose.prod.yml up -d --build
```

Acessa `http://SEU_IP` no navegador. Pronto.

## Atualizar depois (deploy de mudanças)

```bash
ssh root@SEU_IP
cd /opt/roteirizacao
git pull        # (se usou GitHub)
docker compose -f docker-compose.prod.yml up -d --build
```

## Comandos úteis

```bash
docker compose -f docker-compose.prod.yml logs -f app    # logs do Flask
docker compose -f docker-compose.prod.yml logs -f osrm   # logs do OSRM
docker compose -f docker-compose.prod.yml restart app    # reinicia só o Flask
docker compose -f docker-compose.prod.yml down           # para tudo
```

## HTTPS + domínio (opcional, depois)

Pra ter `https://rotas.suaempresa.com` em vez de `http://IP`:
1. Aponta um domínio (registro A) pro IP do servidor
2. Adiciona Caddy ou nginx + certbot como reverse proxy na frente do Flask
   (peço pra configurar quando tiver o domínio)

## Notas

- **OAuth Sheets**: a 1ª vez que clicar "Enviar pra planilha" no servidor,
  o fluxo OAuth precisa de browser. Em servidor headless, copie o
  `dados/sheets_oauth_token.json` já autorizado do seu PC (scp) pra pular
  a autorização interativa.
- **Backup**: o Hetzner oferece snapshots/backup automático (~20% do custo).
  Os dados importantes são `dados/` (cache + config). Faça backup deles.
- **Custo**: CX22 = €4/mês. OSRM usa ~300MB RAM, Flask ~150MB — folga grande.
