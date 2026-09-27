FROM node:18-alpine

WORKDIR /app

COPY package.json ./
RUN npm install --omit=dev --registry=https://registry.npmmirror.com

COPY server.js ./

EXPOSE 1234
CMD ["node", "server.js"]
