# Usamos una imagen de Python ligera basada en Debian Bookworm
FROM python:3.11-slim-bookworm

# Evitar que Python genere archivos .pyc y forzar logs en tiempo real
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Instalar dependencias del sistema: ffmpeg y librerías para Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libmagic1 \
    gcc \
    python3-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Crear directorio de trabajo
WORKDIR /app

# Copiar el archivo de requerimientos primero para aprovechar el cache de Docker
COPY requirements.txt .

# Instalar dependencias de Python
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto del código de la aplicación
COPY ./index.html .
COPY ./server.py .
COPY ./static ./static/
COPY ./routers ./routers

# Crear el directorio donde se guardarán los archivos subidos (opcional)
RUN mkdir -p /app/uploads
RUN mkdir -p /app/outputs

ENV MEDIA_UPLOAD_DIR=/app/uploads
ENV MEDIA_OUTPUT_DIR=/app/outputs

EXPOSE 7070

# Comando para arrancar la aplicación
CMD ["python", "server.py"]
