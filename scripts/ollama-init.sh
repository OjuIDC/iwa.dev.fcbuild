#!/bin/sh
set -eu
until ollama list >/dev/null 2>&1; do sleep 2; done
for model in ${REQUIRED_MODELS}; do
  echo "Ensuring model: $model"
  ollama pull "$model"
done
ollama list
