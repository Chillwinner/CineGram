<div align="center">

# CineGram

### Plugin FastAPI Oficial de Telegram para [CineVec](https://github.com/Chillwinner/Cinevec)

**Telegram es una nube infinita — hasta que intentas encontrar aquel clip de 10 segundos de hace seis meses.**
CineGram transmite fotos y vídeos de hasta 4GB desde tus canales y grupos de Telegram hacia **CineVec** con **Zero-Retention**, para buscarlos semánticamente en 3D y volver al mensaje original `t.me` en un clic.

[English](README.md) · [简体中文](README.zh-CN.md) · [日本語](README.ja.md) · [한국어](README.ko.md) · **Español**

[![CineVec Core](https://img.shields.io/badge/Core-CineVec%20%2Fapi%2Fv1-E06A3E?style=for-the-badge)](https://github.com/Chillwinner/Cinevec)
[![FastAPI](https://img.shields.io/badge/FastAPI-Lightweight%20Web%20UI-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Telethon](https://img.shields.io/badge/MTProto-Telethon%20(Up%20to%204GB)-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)](https://docs.telethon.dev/)

[Qué hace](#qué-hace-cinegram) • [Ventajas](#por-qué-cinegram) • [Núcleo CineVec](#núcleo-compañero-cinevec) • [Extender y Cambiar](#cómo-extender-o-cambiar-componentes) • [Despliegue Rápido](#despliegue-rápido)

</div>

---

## Qué hace CineGram

**CineGram** es un microservicio FastAPI independiente que conecta Telegram con **[CineVec](https://github.com/Chillwinner/Cinevec)** exclusivamente mediante `/api/v1/*` + Bearer API Token, sin acoplarse a su base de datos.

- **4 modos independientes (nada obligatorio)**: ni el Bot Token ni la cuenta MTProto son obligatorios; funciona en modo solo Web, solo Bot, solo MTProto o dual desde su consola ligera (`http://127.0.0.1:8018/`).
- **Importación selectiva**: combina reglas de filtrado por grupo (tipo, tamaño, duración, palabras clave, días) con un **navegador web de miniaturas** para marcar solo los mensajes que quieres importar.
- **Búsqueda semántica y por etiquetas/texto**: integrada tanto en la Web UI como en el Bot opcional (`/s`, `/m` o imagen).

---

## Por qué CineGram

| Característica | Scripts típicos de descarga | CineGram |
| :--- | :--- | :--- |
| **Uso de disco** | Duplican gigabytes de MP4 hasta llenar tu disco. | **Zero-Retention + doble contrapresión**: borra archivos temporales al instante y pausa automáticamente si baja el espacio libre o se satura el preprocesado de CineVec. |
| **Álbumes multiarchivo** | Solo el 1.er archivo conserva el texto y los `#tags`. | **Herencia de texto y `#etiquetas`**: propaga automáticamente la descripción a todos los archivos del álbum. |
| **Enlaces rotos** | Si el grupo cambia de nombre o se vuelve privado, los enlaces mueren. | **Vigilante periódico**: detecta renombres, pasos a privado (`t.me/c/...`) y borrados (`source_invalid`). |
| **Antiduplicados** | Repetir el historial duplica todo. | **4 capas de deduplicación**: bloqueo concurrente + cursor + BD local + consulta `ref_id` en CineVec. |

---

## Núcleo compañero: CineVec

- **Repositorio principal**: [https://github.com/Chillwinner/Cinevec](https://github.com/Chillwinner/Cinevec)
- Genera un Bearer API Token en CineVec (**Settings → Plugin API Tokens**) y pégalo en la consola de CineGram.

---

## Cómo extender o cambiar componentes

- **Sin código**: alterna Bot / MTProto, Zero-Retention / S3, reglas de filtrado e intervalos directamente desde `.env` o la Web UI.
- **Cambiar librería de Telegram o backend destino**: implementa el protocolo `TelegramInspector` en [`src/tg_plugin/models.py`](src/tg_plugin/models.py) o `CineVecClient` en [`src/tg_plugin/cinevec_client.py`](src/tg_plugin/cinevec_client.py) e inyéctalos vía `create_plugin_app(..., inspector=..., cinevec=...)`.

---

## Despliegue rápido

```bash
git clone https://github.com/Chillwinner/CineGram.git
cd CineGram
python -m venv .venv && source .venv/bin/activate
pip install -e .
python -m tg_plugin serve
```

Abre **http://127.0.0.1:8018/**, pega tu CineVec API Token y configura opcionalmente el Bot Token o MTProto (`python -m tg_plugin login`). Tests: `pytest -q`.
