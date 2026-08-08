

<div align="center">

<img src="assets/logo.png" alt="Make It Right" width="360"/>

<h3>La IA lo hace funcionar.&nbsp;&nbsp;Hazlo bien.</h3>

<p><em>Habilidades centradas en restricciones + agentes revisores que impiden a los agentes de código de IA<br/>enviar código backend confiado pero incorrecto.</em></p>

<p>
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/license-Apache_2.0-D4A017"></a>
  <img alt="skills" src="https://img.shields.io/badge/skills-29-555">
  <img alt="runtimes" src="https://img.shields.io/badge/runtimes-9-555">
  <img alt="tools" src="https://img.shields.io/badge/works_with-Claude%20Code%20%7C%20Cursor%20%7C%20Codex%20%7C%20Antigravity-D4A017">
  <a href="EXTENDING.md"><img alt="PRs welcome" src="https://img.shields.io/badge/PRs-welcome-2ea44f"></a>
</p>

</div>

---

La IA es brillante creando código que *funciona*: en el camino feliz, en la demostración, en la prueba que pasa. Es débil creando código que es *correcto*: preciso bajo concurrencia, fallos parciales, reintentos, multiinquilino y datos reales de producción. No son fallos de sintaxis, son **fallos de suposición**. Cuando los requisitos están incompletos, la IA los inventa, con confianza.

**Make It Right** reemplaza "generar y esperar" por **"descubrir restricciones → validar con confirmación → generar → revisar".**

## ¿Por qué tres niveles (y una familia)?

La disciplina de *qué hace que el código sea correcto* es atemporal. Las *trampas del entorno de ejecución* (GIL, bucle de eventos, recolector de basura) son compartidas por todos los frameworks en ese entorno. La mecánica de un *framework* se deteriora a medida que la biblioteca evoluciona. Por eso, Make It Right separa los tres:

- un **pilar agnóstico al lenguaje** (`mir-backend`): la disciplina de pensamiento (validaciones, invariantes),
- un **nivel de entorno de ejecución** (`mir-backend-python`): lo que es cierto para cada framework en ese entorno,
- un **módulo de framework** (`mir-backend-python-fastapi`): las trampas efímeras y específicas de la biblioteca.

El agente carga exactamente los niveles que tu stack necesita (una tarea de FastAPI carga los tres; una tarea de Go cargaría `mir-backend` + `mir-backend-go` + su framework, nunca los niveles de Python). Consulta [EXTENDING.md](EXTENDING.md).

Este repositorio es el **pilar de backend** de una familia planificada. Cada dominio es un hermano que reutiliza el mismo patrón de validación y revisor:

| Estado | Habilidad | Nivel | Cubre |
|---|---|---|---|
| ✅ disponible | `mir-backend` | genérico | Disciplina de backend agnóstica al lenguaje (las validaciones) |
| ✅ disponible | `mir-backend-python` → `-fastapi` · `-django` · `-flask` | entorno + frameworks | CPython (GIL, async/sync, seguridad ante bifurcaciones, inicio en frío) |
| ✅ disponible | `mir-backend-node` → `-express` · `-fastify` · `-nestjs` | entorno + frameworks | Node/V8 (bucle de eventos, hilos trabajadores, contrapresión) |
| ✅ disponible | `mir-backend-jvm` → `-spring` · `-quarkus` · `-micronaut` | entorno + frameworks | JVM (pilas de hilos, hilos virtuales, recolector de basura, inicio en frío) |
| ✅ disponible | `mir-backend-dotnet` → `-aspnetcore` | entorno + framework | .NET CLR (async/await, dependencias capturadas, DbContext) |
| ✅ disponible | `mir-backend-go` → `-gin` · `-fiber` · `-echo` | entorno + frameworks | Go (fugas de goroutine, contexto, condiciones de carrera) |
| ✅ disponible | `mir-backend-php` → `-laravel` · `-symfony` | entorno + frameworks | PHP/Zend (sin recursos compartidos, FPM, fugas de contexto en Octane) |
| ✅ disponible | `mir-backend-ruby` → `-rails` | entorno + framework | Ruby/YARV (GVL, seguridad ante bifurcaciones de Puma, migraciones de AR) |
| ✅ disponible | `mir-backend-rust` → `-axum` · `-actix` | entorno + frameworks | Rust/tokio (bloquear el entorno, guardias a través de await) |
| ✅ disponible | `mir-backend-beam` → `-phoenix` | entorno + framework | BEAM (supervisión, crecimiento del buzón, cuello de botella de GenServer) |
| ✅ disponible | `mir-frontend` → `mir-frontend-react` | pilar + nivel de reactividad | Fiabilidad de UI reactiva (validaciones, contratos UX/estado) + React 19/Compiler (hooks, efectos, Suspense, estado servidor vs cliente) |
| 🔜 planeado | `mir-frontend-react-next` · `-remix` · `-tanstack-start` · `-spa` · `mir-frontend-vue` · `mir-frontend-angular` | módulos/niveles frontend | metaframeworks + otras librerías de reactividad |
| 🔜 planeado | `mir-database` · `mir-data` · `mir-cloud` | pilares | BD, ingeniería de datos, nube |

> **Pilar de frontend:** las validaciones genéricas de `mir-frontend` + el nivel de reactividad `mir-frontend-react` están disponibles ahora (con `a11y-reviewer` + `frontend-perf-reviewer`). Arquitectura completa, referencia de actualidad y hoja de ruta de construcción de metaframeworks: **[docs/frontend-pillar-plan.md](docs/frontend-pillar-plan.md)**.

## ¿Qué hay en este repositorio?

```
skills/
  mir-backend/                  # genérico: la disciplina de pensamiento (validaciones, interrogatorio)
    SKILL.md
    references/                 # catálogo de restricciones, catálogo de modos de fallo, listas de verificación, mapa de entorno
  mir-backend-python/           # nivel de entorno: preocupaciones de CPython (GIL, async/sync, seguridad ante bifurcaciones, inicio en frío)
    SKILL.md
  mir-backend-python-fastapi/   # módulo de framework: FastAPI + Async SQLAlchemy 2.0 + Postgres + Alembic + Redis
    SKILL.md
    references/                 # trampas de fastapi (código correcto/incorrecto), seguridad en migraciones de alembic
agents/
  constraint-interrogator.md  # propone las 2-4 preguntas de mayor impacto (con valores predeterminados)
  reliability-reviewer.md     # idempotencia, fallo parcial, concurrencia, observabilidad
  security-reviewer.md        # IDOR/BOLA, asignación masiva, aislamiento de inquilinos, SSRF, fugas
  migration-reviewer.md       # migraciones seguras en tablas con datos (expansión/contracción)
```

## El flujo de trabajo (con validación estricta)

```
Gate 0  Intención y Triaje          restablecer la intención real, clasificar superficie de riesgo
Gate 1  Interrogatorio de Restricciones  interrogador → preguntar al usuario 2-4 preguntas con [DEFAULT]   [VALIDACIÓN DE USUARIO]
Gate 2  Registro de Suposiciones      escribir suposiciones → usuario confirma             [VALIDACIÓN DE USUARIO]
Gate 3  Invariantes y Fallos          invariantes, máquina de estados, modos de fallo
Gate 4  Registro de Riesgos           Riesgo | Gravedad | Probabilidad | Mitigación
Gate 5  Revisión de Diseño            límites de transacciones, consistencia, observabilidad      [VALIDACIÓN DE USUARIO]
──────────── ahora se puede escribir código ────────────
Gate 6  Implementación                contra la lista de verificación de generación de código
Gate 7  Listo para Producción         revisores en paralelo → corregir Crítico/Alto
```

**No se escribe código de implementación hasta que se apruebe Gate 5.** Tres validaciones requieren entrada explícita del usuario: el agente nunca las aprueba por sí mismo.

## ¿Por qué subagentes?

Un subagente no puede hacerte preguntas: vuelve al orquestador. Por lo tanto:
- el **constraint-interrogator** realiza el barrido ruidoso del catálogo fuera del contexto principal y devuelve solo las preguntas distiladas (cada una con un valor predeterminado recomendado + fundamento); el orquestador te pregunta a *ti* a través de la interfaz de preguntas.
- los **revisores** son de solo lectura (sin herramientas de edición): *informan* hallazgos etiquetados por gravedad; el orquestador realiza el triaje y las correcciones. Esto mantiene la "decisión sobre lo importante" bajo tu control.

Refleja el patrón de 2026: `Planner → Architect → Implementer → Reliability Reviewer → Security Reviewer`, reemplazando el enfoque de un solo paso "prompt → volcado masivo de código".

## Instalación

```bash
git clone <your-fork-url> make-it-right
cd make-it-right
./install.sh                      # predeterminado: Claude Code (~/.claude)
./install.sh --tool=cursor        # Cursor (lee recursos de ~/.claude)
./install.sh --tool=codex         # Codex CLI (AGENTS.md → ~/.codex)
./install.sh --tool=antigravity   # Antigravity (habilidades → ~/.gemini/antigravity, AGENTS.md → ~/.gemini)
./install.sh --tool=all           # todo
```

Enlaces simbólicos, no copias: los cambios en el repositorio están disponibles inmediatamente. Anula los destinos con `CLAUDE_HOME` / `CODEX_HOME` / `GEMINI_HOME`.

## Compatible con cuatro agentes

El ecosistema ha convergido en un único modelo: *habilidades* (`SKILL.md` cargado bajo demanda por nombre+descripción), *subagentes*, *hooks* y el estándar cruzado **`AGENTS.md`**. Make It Right entrega un **núcleo agnóstico a la herramienta**; el cuerpo de las habilidades se degrada de forma elegante donde una herramienta carece de una característica.

| | Habilidades | Subagentes | Interfaz de preguntas | Cómo se carga |
|---|---|---|---|---|
| **Claude Code** | ✅ nativo | ✅ nativo | clicable (`AskUserQuestion`) | `~/.claude/{skills,agents}` |
| **Cursor** | ✅ | ✅ | texto plano | lee `~/.claude/{skills,agents}` + `AGENTS.md` |
| **Codex CLI** | ✅ (`/skills`) | ✅ (config) | texto plano | base `AGENTS.md`; habilidades vía `/skills` |
| **Antigravity** | ✅ (`SKILL.md`) | personas / en línea | texto plano / plan | `~/.gemini/antigravity/skills` + `AGENTS.md` |

Los dos mecanismos específicos de Claude, la herramienta de preguntas clicable y la distribución de subagentes, están escritos como alternativas elegantes: en otras herramientas, el agente hace preguntas en texto plano y ejecuta las listas de verificación de los revisores en línea. La *disciplina* (validaciones, regla estricta, catálogos) es idéntica en todas partes. Solo con `AGENTS.md` se obtiene una línea base funcional en los cuatro.

## Uso

```
/mir-backend Build an order checkout endpoint that charges a card and decrements inventory
```

El agente restablece tu intención, hace 2-4 preguntas precisas con valores predeterminados recomendados, escribe un Registro de Suposiciones para que lo confirmes, produce un registro de riesgos y un diseño para aprobación, *luego* implementa y finalmente ejecuta a los revisores.

Banderas: `--advisory` (validación suave, continúa con los valores predeterminados) · `--skip-interrogation` (omite la ronda de preguntas, aún requiere confirmación del registro).

Para proyectos de FastAPI, `/mir-backend-python-fastapi` se carga automáticamente en las validaciones de diseño/implementación.

## Composición con otros frameworks de planificación

Make It Right es la **capa de planificación específica para backend**. Si usas un planificador más amplio (p. ej., GSD o una habilidad de plan maestro), ejecuta esto *dentro* de la planificación de una fase de backend: genera el Registro de Suposiciones y el Registro de Riesgos que el plan de la fase debería citar. Hace referencia a esos frameworks en lugar de duplicarlos.

## Personalización

- Ajusta el banco de preguntas de restricciones en `skills/mir-backend/references/constraint-catalog.md` para las invariantes recurrentes de tu dominio.
- Ajusta los umbrales de gravedad y los elementos de la lista de verificación en `skills/mir-backend/references/checklists.md`.
- Agrega un nuevo módulo de framework o pilar (`mir-backend-express`, `mir-frontend`, `mir-database`, …) siguiendo **[EXTENDING.md](EXTENDING.md)**: cubre el modelo de carga perezosa, la regla de descripción `TRIGGER`/`SKIP` que evita la carga de habilidades no relacionadas, la convención de nomenclatura con prefijo de pilar y recetas de copiar y pegar.

## Licencia

Bajo licencia [Apache License 2.0](LICENSE) — © 2026 Anant Bhandarkar. Úsalo, haz un fork, distribúyelo; contribuciones bienvenidas.
