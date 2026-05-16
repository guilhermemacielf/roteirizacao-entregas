# Imagem mínima com osmium-tool pra recortar PBF da OSM.
# Build:  docker build -t local/osmium-tool -f osmium.Dockerfile .
# Uso:    docker run --rm -v "$PWD/data:/data" local/osmium-tool extract --bbox=W,S,E,N /data/sudeste-latest.osm.pbf -o /data/rmbh.osm.pbf

FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends osmium-tool ca-certificates \
 && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["osmium"]
