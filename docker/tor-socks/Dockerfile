FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends tor ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && printf '%s\n' \
        'SocksPort 0.0.0.0:9050' \
        'SocksTimeout 60' \
        'CircuitBuildTimeout 15' \
        'LearnCircuitBuildTimeout 0' \
        'Log notice stdout' \
        > /etc/tor/torrc
USER debian-tor
EXPOSE 9050
CMD ["tor", "-f", "/etc/tor/torrc"]
