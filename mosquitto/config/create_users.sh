#!/bin/bash
# Script to create/update Mosquitto users
# Usage: ./create_users.sh

# Create password file with admin user (password: password1234)
docker run --rm -v "$PWD/mosquitto/config:/mosquitto/config" eclipse-mosquitto mosquitto_passwd -c /mosquitto/config/passwd admin

echo "Created password file with admin user"
echo "Default password: password1234"
echo "Change password with: docker run --rm -v \"\$PWD/mosquitto/config:/mosquitto/config\" eclipse-mosquitto mosquitto_passwd /mosquitto/config/passwd admin"