# Probar macOS 26 (Tahoe) gratis

Repositorio mínimo para **ejecutar macOS 26 en un runner gratuito de GitHub Actions**
(Apple Silicon, con GPU). Es headless (sin escritorio gráfico): sirve para verificar
que macOS 26 funciona, inspeccionar el hardware/SoC y probar Metal/GPU.

## Cómo usarlo

1. El workflow se ejecuta automáticamente al hacer push y también puedes lanzarlo
   manualmente desde la pestaña **Actions** → *Probar macOS 26* → **Run workflow**.
2. En los logs verás: versión de macOS, chip, memoria, GPU y capacidades de Metal.

## Notas

- Gratis porque el repositorio es **público** y el runner es el público de Apple Silicon.
- No hay interfaz gráfica ni remote desktop. Si quieres ver/usar el escritorio de
  macOS 26, no hay opción gratuita (mínimo: prueba de $0.99/24 h en MacinCloud).
