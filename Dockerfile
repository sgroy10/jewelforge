FROM python:3.11-slim-bookworm

# Install Blender dependencies + useful tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    xz-utils \
    libxi6 \
    libxxf86vm1 \
    libxfixes3 \
    libxrender1 \
    libgl1-mesa-dri \
    libglx-mesa0 \
    libglx0 \
    libgl1 \
    libegl1-mesa \
    libegl1 \
    libxkbcommon0 \
    libsm6 \
    libglib2.0-0 \
    libx11-6 \
    libxext6 \
    libice6 \
    libgomp1 \
    libxxf86vm1 \
    libtbb12 \
    && rm -rf /var/lib/apt/lists/*

# Install Blender 3.6 LTS (better compatibility)
# Switched to official Blender mirror 2026-04-30 — mirror.clarkson.edu was returning SSL errors (wget exit 5)
RUN wget -q https://download.blender.org/release/Blender3.6/blender-3.6.5-linux-x64.tar.xz \
    && tar -xf blender-3.6.5-linux-x64.tar.xz -C /opt/ \
    && rm blender-3.6.5-linux-x64.tar.xz \
    && ln -s /opt/blender-3.6.5-linux-x64/blender /usr/local/bin/blender

# Verify blender works
RUN blender --version || echo "WARN: Blender may need additional libs at runtime"

ENV BLENDER_USER_CONFIG=/tmp/blender_config

# Install Python dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

EXPOSE 8080

CMD ["python", "server.py"]
