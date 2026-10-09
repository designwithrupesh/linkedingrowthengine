FROM node:22-alpine@sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402
WORKDIR /usr/src/app
COPY --chown=node:node main.mjs ./main.mjs
USER node
CMD ["node", "/usr/src/app/main.mjs"]
