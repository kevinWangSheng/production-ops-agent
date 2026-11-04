# autossh sidecar for the 72h soak (scripts/soak/compose.soak.yaml). Built on
# da-server only; pinned base and packages from that Alpine release.
FROM alpine:3.21.3
ARG SOAK_UID
ARG SOAK_GID
RUN apk add --no-cache autossh openssh-client \
    && addgroup -g "${SOAK_GID}" soak \
    && adduser -D -H -u "${SOAK_UID}" -G soak -h /tmp soak
USER soak
ENTRYPOINT ["autossh"]
