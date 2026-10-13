ARG ACCEPTANCE_RUN_ID
ARG SOURCE_REVISION
FROM scratch
ARG ACCEPTANCE_RUN_ID
ARG SOURCE_REVISION
COPY xray /usr/local/bin/xray
ENV PATH="/usr/local/bin:/usr/bin:/bin"
LABEL io.fwrouter.acceptance.owner="fwrouter-test-harness-v1"
LABEL io.fwrouter.acceptance.run="${ACCEPTANCE_RUN_ID}"
LABEL org.opencontainers.image.revision="${SOURCE_REVISION}"
ENTRYPOINT ["/usr/local/bin/xray"]
