# Use official Python image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_HOME="/opt/poetry" \
    PATH="/opt/poetry/bin:$PATH"

# Install curl and build tools
RUN apt-get update && apt-get install -y curl build-essential && \
    curl -sSL https://install.python-poetry.org | python3 - && \
    poetry --version && \
    apt-get remove -y curl && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Container timezone defaults to IST
RUN echo "Asia/Kolkata" > /etc/timezone

# Copy only Poetry files first to install dependencies
COPY pyproject.toml poetry.lock* ./

# Install dependencies (no venv)
RUN poetry config virtualenvs.create false \
    && poetry install --no-interaction --no-ansi

# Copy the rest of the application
COPY . .

# Expose the required port
EXPOSE 59966

VOLUME /app/db
VOLUME /app/in
VOLUME /app/out

# Set default command
CMD ["poetry", "run", "python3", "-m", "src.main"]
