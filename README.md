# Video Chunker Worker

Worker serverless en Python para dividir un video de Google Cloud Storage en clips MP4 usando `ffmpeg`.

## Input

RunPod Serverless espera que el payload vaya dentro de `input`:

```json
{
  "input": {
    "uri": "gs://my-bucket/videos/source.mp4",
    "clippingId": "clipping_123",
    "splittingSeconds": 60,
    "overlappingSeconds": 10
  }
}
```

El input interno del worker es:

```json
{
  "uri": "gs://my-bucket/videos/source.mp4",
  "clippingId": "clipping_123",
  "splittingSeconds": 60,
  "overlappingSeconds": 10
}
```

`uri` puede ser `gs://bucket/path/video.mp4` o `https://storage.googleapis.com/bucket/path/video.mp4`.

`splittingSeconds` es la duración objetivo de cada clip. `overlappingSeconds` es el solape con el siguiente clip y debe ser menor que `splittingSeconds`. Si no se envía, se usa `0`.

## Output

```json
{
  "clips": [
    "gs://my-bucket/videos/clipping/clipping_123/000000.mp4",
    "gs://my-bucket/videos/clipping/clipping_123/000001.mp4"
  ],
  "metadata": [
    { "start": 0, "end": 60, "size": 123456 },
    { "start": 50, "end": 110, "size": 123456 }
  ]
}
```

En RunPod, este objeto se devuelve como el output del job.

Los clips se guardan en el mismo bucket, bajo el directorio del video original:

```text
gs://bucket/path/to/clipping/{clippingId}/{clipId}.mp4
```

Por ejemplo, para `gs://bucket/path/to/video.mp4`:

```text
gs://bucket/path/to/clipping/clipping_123/000000.mp4
```

## Cómo Funciona

1. Descarga el objeto completo desde GCS a `/tmp`.
2. Calcula la duración del video con `ffprobe`.
3. Calcula rangos solapados. Por ejemplo, con `splittingSeconds=60` y `overlappingSeconds=10`: `0-60`, `50-110`, `100-160`, etc.
4. Genera clips locales con IDs ordenables: `000000`, `000001`, etc.
5. Sube cada clip a GCS.
6. Devuelve las URIs y metadata `{ start, end, size }`.

Los clips se reencodean con H.264/AAC para que la duración real de cada MP4 respete el rango calculado. Esto es más lento que copiar streams con `-c copy`, pero evita clips más largos por alineación a keyframes.

## Local

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8080
```

Ejemplo:

```bash
curl -X POST http://localhost:8080/split \
  -H 'Content-Type: application/json' \
  -d '{
    "uri": "gs://my-bucket/videos/source.mp4",
    "clippingId": "clipping_123",
    "splittingSeconds": 60,
    "overlappingSeconds": 10
  }'
```

## RunPod Serverless

El `Dockerfile` arranca `handler.py`, que usa el contrato nativo de RunPod:

```python
def handler(job):
    request = job["input"]
    ...
```

Build recomendado para subir a un registry:

```bash
docker build --platform linux/amd64 -t your-docker-user/video-chunker:latest .
docker push your-docker-user/video-chunker:latest
```

En RunPod, configura el secret `GOOGLE_CREDENTIALS_JSON` con el contenido completo del JSON de la service account. El worker también soporta `GOOGLE_APPLICATION_CREDENTIALS` si prefieres montar un archivo.

Ejemplo de request a `/runsync`:

```json
{
  "input": {
    "uri": "https://storage.googleapis.com/my-bucket/videos/source.mp4",
    "clippingId": "clipping_123",
    "splittingSeconds": 60,
    "overlappingSeconds": 10
  }
}
```

## Docker Local

```bash
docker build -t video-chunker .
docker run --rm -e GOOGLE_CREDENTIALS_JSON="$GOOGLE_CREDENTIALS_JSON" video-chunker
```

Para probar la API HTTP local, ejecuta `uvicorn main:app --host 0.0.0.0 --port 8080` en vez del handler de RunPod.
