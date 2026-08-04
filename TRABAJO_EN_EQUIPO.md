# Cómo trabajamos las ramas

Guía para que los dos avancemos sin pisarnos y sin romper lo que ya
funciona.

---

## Estado de las ramas

| Rama | Qué tiene |
|---|---|
| `master` | El backend original. La IA **no** estaba conectada: `/query` devolvía la cadena `"[]"`. |
| `joshua` | Todo lo nuevo: el agente que sí piensa, la UI de prueba y 5 bugs corregidos. Es la que hay que usar. |

`main` existe pero está igual que `master`. Ignórala.

---

## Paso 1 — Ver qué cambió

Antes de tocar nada, mira la diferencia:

```powershell
git clone https://github.com/Kaquenduri/paquitobot-rag.git
cd paquitobot-rag
git fetch origin

# Resumen: qué archivos cambiaron y cuánto
git diff master..origin/joshua --stat

# El detalle de un archivo puntual
git diff master..origin/joshua -- app/services/rag_service.py

# Los commits que tiene joshua y master no
git log master..origin/joshua --oneline
```

Si prefieres verlo en el navegador:
`https://github.com/Kaquenduri/paquitobot-rag/compare/master...joshua`

**Lo importante que vas a encontrar en el diff:**

- `app/rag/agente.py` — el corazón. El modelo recibe un catálogo de
  herramientas y decide cuál usar. Antes había un planificador de regex
  que se rompía si el alumno escribía "moviles avanzados" en vez de
  "Móviles Avanzado".
- `app/rag/llm.py` — cliente de chat compatible con OpenAI. Sirve igual
  para Ollama local que para una API de pago; se cambia con 3 variables
  del `.env`, sin tocar código.
- `app/canvas/dto.py` — el bug que impedía sincronizar. `important_dates`
  estaba declarado como diccionario y Canvas manda un booleano; las 313
  tareas fallaban la validación y el sync moría con `schema_drift`.
- `app/text_to_sql/executor.py` — `SET LOCAL app.tenant_id = :param` es
  error de sintaxis en Postgres. El ejecutor nunca había funcionado
  contra una base real.
- `app/web/index.html` — la UI de prueba.

---

## Paso 2 — Que te funcione

Sigue **[ARRANQUE.md](ARRANQUE.md)** de arriba a abajo, sin saltarte pasos.
Está escrito solo con comandos ya probados. Los tres puntos donde es fácil
tropezar:

1. Instala `requirements-app.txt`, **no** `requirements.txt`. El completo
   trae `chromadb` y `onnxruntime`, que solo usa el `src/` viejo y son los
   que revientan al compilar en Windows.
2. Antes de migrar, exporta la variable a mano. `alembic/env.py` lee la
   variable de entorno, **no** el `.env`, y si no la pones se conecta a
   otro Postgres.
3. Necesitas **tu propio token de Canvas**. El mío no te sirve.

Cuando el chat te responda algo con tus notas reales, ya estás listo.

---

## Paso 3 — Trabajar sin pisarnos

**Regla base: nadie trabaja directo sobre `joshua`.** Cada uno saca su
rama, y se junta por Pull Request.

```powershell
# Parte SIEMPRE desde joshua actualizado
git checkout joshua
git pull origin joshua

# Tu rama para lo que vas a hacer
git checkout -b <tu-nombre>-<lo-que-haces>
```

Ejemplos de nombres: `luis-oauth`, `luis-notificaciones`,
`joshua-frontend-movil`.

Trabajas, y antes de subir:

```powershell
# 1. Que no hayas roto nada (deben pasar 204)
.\.venv\Scripts\python.exe -m pytest -q

# 2. Que el chat siga respondiendo
#    Abre http://127.0.0.1:8000/dev/ y prueba una pregunta

# 3. Recién ahí
git add -A
git commit -m "feat: lo que hiciste"
git push -u origin <tu-rama>
```

Después entras a GitHub y abres el Pull Request **hacia `joshua`**, no
hacia `master`. El otro lo revisa y lo mergea.

---

## Paso 4 — Mantenerse al día

Cuando el otro mergee algo a `joshua`, tráelo a tu rama antes de seguir:

```powershell
git checkout joshua
git pull origin joshua

git checkout <tu-rama>
git merge joshua
```

Hazlo seguido. Mientras más esperas, peor el conflicto.

---

## Si hay conflicto

```powershell
git merge joshua
# CONFLICT (content): Merge conflict in app/rag/agente.py
```

Abre el archivo, busca las marcas `<<<<<<<`, `=======`, `>>>>>>>`, deja el
código que corresponde, borra las marcas y:

```powershell
git add app/rag/agente.py
git commit
```

Si te enredaste y quieres empezar de nuevo:

```powershell
git merge --abort
```

---

## Reglas que nos ahorran problemas

**Nunca subas el `.env`.** Está en `.gitignore`, pero si alguna vez lo
fuerzas con `git add -f`, estarías publicando las llaves de cifrado y el
token de Canvas. Tampoco pegues tokens en el código ni en el chat.

**Corre los tests antes de cada push.** Deben pasar 204. Si de repente
pasan menos, algo rompiste. Los 9 que fallan son de antes y son del
entorno: `tests/conftest.py` apunta a una ruta de Python de otra máquina y
`test_chroma_db` necesita el stack viejo.

**`DEV_UI_ENABLED` va apagado en cualquier cosa que no sea tu máquina.**
Ese endpoint reparte sesiones de alumno sin pedir contraseña. Es andamio
de desarrollo y desaparece cuando exista el login real.

**Un commit, una cosa.** Es más fácil revisar y más fácil revertir si sale
mal.

---

## Qué falta hacer (para repartirnos)

1. **OAuth de Canvas.** Hoy cada alumno tiene que pegar su token a mano.
   Lo correcto es un botón "Conectar con Canvas". Requiere que TI de
   Tecsup nos cree una *Developer Key*, así que ese trámite hay que
   empezarlo ya: es el que más demora.
2. **Modelo mejor.** `qwen2.5:3b` en CPU tarda entre 45 s y 2 minutos por
   respuesta y a veces se equivoca de curso cuando hay que saltar de
   idioma ("cloud" → "Nube"). El código ya está listo para apuntar a una
   API; son 3 líneas del `.env`.
3. **App móvil.** La UI actual es una carcasa de teléfono en el navegador
   para probar. El producto va a ser móvil.
4. **Promedio del curso.** Ojo con esto: `computed_current_score` de
   Canvas es un porcentaje ponderado y puede pasar de 100 (vi 132 y 118).
   El promedio vigesimal hay que calcularlo desde `submissions.score`.
5. **Notificaciones.** Que avise antes de que venza algo, sin que el
   alumno pregunte. Los datos ya están sincronizados; falta el disparador.
