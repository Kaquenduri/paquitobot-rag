# Arrancar PaquitoBot en tu máquina

Guía para levantar el backend, conectar tu Canvas de Tecsup y chatear con
la IA. Todos los comandos de acá se ejecutaron y funcionaron en Windows 11
con Python 3.13. Si algo no está en esta guía, es porque no lo probé.

**Tiempo:** unos 20 minutos, casi todo esperando descargas.

---

## Qué vas a tener al final

Un chat que responde sobre **tus** cursos reales de Tecsup:

> **Tú:** cuanto saqe en la semana 2 de moviles avanzados
> **Paquito:** En la semana 2 sacaste 20 de 20 en el Lab 02 - Estructuras
> Condicionales y Bucles en Swift del curso Programación en Móviles Avanzado.

Corre entero en tu máquina. No se paga nada y ningún dato sale de tu PC.

---

## 0. Lo que necesitas instalado

| Qué | Para qué | Cómo verificar |
|---|---|---|
| Python 3.13 | el backend | `python --version` |
| Docker Desktop | la base de datos | `docker --version` |
| Ollama | la IA local | `ollama --version` |

Si te falta Ollama:

```powershell
winget install Ollama.Ollama
```

---

## 1. Código y dependencias

```powershell
git clone https://github.com/Kaquenduri/paquitobot-rag.git
cd paquitobot-rag
git checkout joshua

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-app.txt
```

Usa **`requirements-app.txt`**, no `requirements.txt`. El completo trae
`chromadb` y `onnxruntime`, que solo usa el pipeline viejo de `src/` y son
los que fallan al compilar en Windows.

---

## 2. Base de datos

```powershell
docker run -d --name paquito-pg `
  -e POSTGRES_PASSWORD=paquito -e POSTGRES_USER=paquito -e POSTGRES_DB=paquito `
  -p 5433:5432 postgres:16-alpine
```

El puerto es **5433** a propósito: si ya tienes un Postgres local en el
5432, no chocan.

---

## 3. Archivo `.env`

Primero genera tus propios secretos:

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; import secrets; print('TENANT_TOKEN_KEY=' + Fernet.generate_key().decode()); print('BACKEND_SECRET=' + secrets.token_urlsafe(48))"
```

Crea un archivo `.env` en la raíz del proyecto con esto, pegando los dos
valores que te imprimió el comando anterior:

```ini
SUPABASE_DATABASE_URL=postgresql+psycopg://paquito:paquito@127.0.0.1:5433/paquito

TENANT_TOKEN_KEY=<pega aquí el que generaste>
BACKEND_SECRET=<pega aquí el que generaste>

CANVAS_API_BASE_URL=https://tecsup.instructure.com/api/v1

LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_MODEL=qwen2.5:3b
LLM_API_KEY=ollama
LLM_TIMEOUT_SECONDS=300

OLLAMA_HOST=http://127.0.0.1:11434

LOG_LEVEL=INFO
SCHEDULER_ENABLED=false
DEV_UI_ENABLED=true
```

`.env` está en `.gitignore`. **Nunca lo subas.**

---

## 4. Crear las tablas

Ojo con esto: `alembic/env.py` lee la **variable de entorno**, no el
archivo `.env`. Hay que exportarla a mano o se conecta al Postgres
equivocado:

```powershell
$env:SUPABASE_DATABASE_URL = "postgresql+psycopg://paquito:paquito@127.0.0.1:5433/paquito"
.\.venv\Scripts\alembic.exe upgrade head
```

Debe decir `Running upgrade -> 0001_init`.

---

## 5. La IA

```powershell
ollama serve
```

Déjalo abierto en su propia terminal. En **otra** terminal:

```powershell
ollama pull qwen2.5:3b
```

Son ~1.9 GB. Es el modelo chico y rápido; soporta *tool calling*, que es
lo que necesita el agente. Si tu PC tiene 16 GB de RAM o más y prefieres
mejores respuestas a cambio de esperar el doble, usa `qwen2.5:7b` y cambia
`LLM_MODEL` en el `.env`.

**No uses los modelos `deepseek-r1`**: son de razonamiento, "piensan en voz
alta" antes de responder y en CPU son lentísimos.

---

## 6. Arrancar el backend

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Verifica en otra terminal:

```powershell
curl http://127.0.0.1:8000/healthz
```

---

## 7. Conectar tu Canvas y traer tus datos

Necesitas **tu propio token** de Canvas: entra a
`https://tecsup.instructure.com` → Cuenta → Configuración → *Nuevo token de
acceso*. Cópialo apenas lo generes, después ya no se puede ver.

```powershell
$env:CANVAS_TOKEN = "<tu token de Canvas>"

# 1. Un JWT de sesión
$jwt = (Invoke-RestMethod -Method Post http://127.0.0.1:8000/dev/session).token

# 2. Guardar tu token de Canvas (queda cifrado en la base)
Invoke-RestMethod -Method Post http://127.0.0.1:8000/auth/canvas/connect `
  -Headers @{ Authorization = "Bearer $jwt"; "X-Canvas-Token" = $env:CANVAS_TOKEN }

# 3. Sincronizar (tarda 1-3 minutos)
Invoke-RestMethod -Method Post http://127.0.0.1:8000/sync `
  -Headers @{ Authorization = "Bearer $jwt" }
```

El paso 3 debe devolver `status: ok`. Para confirmar qué entró:

```powershell
docker exec paquito-pg psql -U paquito -d paquito -c "select count(*) from assignments;"
```

---

## 8. Probar

Abre **http://127.0.0.1:8000/dev/**

Pregúntale cosas como:

- `cuanto saqe en la semana 2 de <algún curso tuyo>`
- `como voy en <curso>`
- `que venció en junio`
- `todas mis notas de <curso>`

---

## Qué esperar (no es un bug)

**Cada respuesta tarda entre 45 segundos y 2 minutos.** El agente hace 2 o
3 pasadas completas por el modelo, y en CPU sin GPU dedicada eso es lo que
cuesta. La pantalla muestra los tres puntitos mientras piensa.

**El modelo de 3B se equivoca a veces de curso**, sobre todo cuando hay que
dar un salto de idioma (le dices "cloud" y el curso se llama "Nube"). Está
diseñado para que cuando pase, la respuesta **nombre el curso que realmente
consultó** — así el error se ve en vez de pasar colado. Si la respuesta
menciona un curso que no es el que preguntaste, es eso.

Las dos limitaciones se arreglan igual: con un modelo mejor. La
arquitectura no cambia, solo `LLM_BASE_URL`, `LLM_MODEL` y `LLM_API_KEY`.

---

## Si algo falla

| Síntoma | Causa | Solución |
|---|---|---|
| `No module named 'greenlet'` al migrar | falta la dependencia | `pip install greenlet==3.1.1` |
| `password authentication failed for user "postgres"` | alembic no leyó tu `.env` | exporta `$env:SUPABASE_DATABASE_URL` (paso 4) |
| `alembic: No module named alembic.__main__` | invocación equivocada | usa `.\.venv\Scripts\alembic.exe`, no `python -m alembic` |
| `/sync` devuelve `schema_drift` | DTO desactualizado vs Canvas | asegúrate de estar en la rama `joshua` |
| `/query` da 500 con `IdleInTransactionSessionTimeout` | el modelo tardó con una transacción abierta | ya está arreglado en `joshua`; verifica tu rama |
| `/dev/` no carga | falta la bandera | `DEV_UI_ENABLED=true` en el `.env` |
| Chat responde "No pude conectarme" | Ollama apagado | `ollama serve` en otra terminal |
| Respuestas dicen `[FAKE]` | apuntando al servidor de pruebas | `LLM_BASE_URL=http://127.0.0.1:11434/v1` |

---

## Aviso de seguridad

`DEV_UI_ENABLED=true` monta `/dev`, que **reparte sesiones de alumno sin
pedir contraseña**. Es andamio de desarrollo. Está apagado por defecto y no
debe encenderse en ningún despliegue real: cuando exista el login de
verdad, ese endpoint desaparece.

---

## Para detener todo

```powershell
docker stop paquito-pg          # la base (docker start paquito-pg para volver)
```

Y Ctrl-C en las terminales de uvicorn y de ollama.
