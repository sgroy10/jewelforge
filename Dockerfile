FROM python:3.11-slim

# Install Blender and dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    xz-utils \
    libxi6 \
    libxxf86vm1 \
    libxfixes3 \
    libxrender1 \
    libgl1-mesa-glx \
    libxkbcommon0 \
    libsm6 \
    && rm -rf /var/lib/apt/lists/*

# Install Blender 4.0
RUN wget -q https://mirror.clarkson.edu/blender/release/Blender4.0/blender-4.0.2-linux-x64.tar.xz \
    && tar -xf blender-4.0.2-linux-x64.tar.xz -C /opt/ \
    && rm blender-4.0.2-linux-x64.tar.xz \
    && ln -s /opt/blender-4.0.2-linux-x64/blender /usr/local/bin/blender

# Install Python dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

EXPOSE 8080

CMD ["python", "server.py"]
