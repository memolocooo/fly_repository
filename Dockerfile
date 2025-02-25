# syntax=docker/dockerfile:1

# Set Python version
ARG PYTHON_VERSION=3.8.10
FROM python:${PYTHON_VERSION}-slim

# Upgrade pip to avoid issues
RUN pip install --upgrade pip

# Set Flask runtime label (optional)
LABEL fly_launch_runtime="flask"

# Set working directory
WORKDIR /code

# Copy and install dependencies
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Expose Fly.io default port
EXPOSE 8080

# Set Flask environment variables
ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0
ENV FLASK_RUN_PORT=8080

# Run the app
CMD ["python3", "-m", "flask", "run", "--host=0.0.0.0", "--port=8080"]


RUN pip install chardet
FROM python:3.9-slim 

RUN pip install python-amazon-sp-api





