# Usa a imagem oficial do Nginx
FROM nginx:alpine

# Remove o conteúdo padrão
RUN rm -rf /usr/share/nginx/html/*

# Copia o HTML e o JSON para a pasta servida pelo Nginx
COPY dashboard /usr/share/nginx/html/dashboard
COPY data/reports /usr/share/nginx/html/data/reports

# Copia configuração personalizada do Nginx (se precisar)
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Expor a porta 80
EXPOSE 80

# Comando definido pela imagem
CMD ["nginx", "-g", "daemon off;"]