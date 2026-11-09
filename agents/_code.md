---
abstract: true
extends: _base
---
{{EXTENSION}}

## Test cadence — narrow while working, the full suite once per handover

Set by the user on 2026-11-09, after a ten-minute job took two hours because the whole stack's
suite ran every few minutes.

- **While you code, run only the tests for what you are touching** — one file, one `-k`, one
  package. Seconds, not minutes.
- **No test gate on a commit or a finished task.** Commit each logical step; do not run the
  full suite to do it.
- **The full suite runs ONCE, when you are about to hand back to the lead** — after all your
  tasks, not after each. Red? Fix it then, rerun, and hand back with the actual result. Nothing
  the suite covers changed (docs, config, agent text)? Do not run it at all, and say so.

## Code standard — how code on this team is written

Set by the user on 2026-09-15, for every language. It applies to code you write and to code you
review; the repo's own conventions still win where they are stricter. Your stack layer above names
this language's concrete forms.

- **No call nested inside another call's arguments.** Give the inner result a name on its own
  line, then pass the name. A fluent chain (`query.Where(...).OrderBy(...)`), one call per line,
  is fine; a call buried in an argument list is not — it hides a step, an `await` and a failure
  point inside one line.

  ```csharp
  // no
  db.DomainCertificates.RemoveRange(await db.DomainCertificates.Where(c => c.DomainId == id).ToListAsync(ct));
  ```

- **An array, not a list, unless the items change.** Data that is read, returned, passed on or
  iterated is materialised as the language's fixed or read-only collection. A growable list is
  for code that adds, removes or reorders — its type is the signal that something will.
- **A bulk change runs in the database as one statement.** Never load rows to delete or update
  them one by one: issue the set-based delete or update directly. When a unit of work needs more
  than one statement that must succeed together, wrap them in one explicit transaction. A
  set-based statement bypasses the ORM's change tracker, save hooks and interceptors — if the
  project relies on those (soft delete, audit, domain events), that is the reason to keep the
  tracked path, and you say so in a comment.

  ```csharp
  // yes
  await db.DomainCertificates
      .Where(c => c.DomainId == id)
      .ExecuteDeleteAsync(ct);
  ```

### Data access

- **No query inside a loop.** An N+1 is a loop that talks to the database; fetch the set in one
  query (`WHERE id IN (...)`, a join, an include) and work on it in memory.
- **Filter, sort and project in the database.** Never load a table and filter it in memory. Select
  the columns the caller needs, not the whole entity, and read without change tracking when
  nothing will be saved (`AsNoTracking` and its equivalents).
- **Every list query is bounded** — a limit or pagination. An unbounded query is fast in
  development and an outage in production.
- **SQL is always parameterised.** Never build a statement by interpolation or concatenation,
  even from a value you believe is safe.

### Shape of the code

- **Guard clauses and early returns, not nesting.** Handle the invalid case first and return; the
  main path stays at the left margin. Two levels of nesting is the ceiling.
- **A function does one thing**, and its name says what. No boolean parameter that switches its
  behaviour — that is two functions.
- **No magic numbers or strings.** A literal with meaning gets a named constant or an enum.
- **Immutable by default** — `readonly`, `const`, `val`, `final`, records for DTOs. A mutable
  binding says it will change; make that true.
- **Match exhaustively on enums and closed types** (`switch` expressions, `match`, `when`) rather
  than if/else chains, and fail loudly on a value you did not expect.
- **Names are words.** No abbreviations beyond the domain's own; booleans read as `is` / `has` /
  `can`.

### Failure and resources

- **Never swallow an exception.** Catch only what you can handle here; no empty `catch`, no catch
  that logs and carries on as if nothing happened.
- **Every resource is disposed** by the language's construct — `using`, `with`, `defer`, `use`,
  try-with-resources — never by a close call someone has to remember.
- **No fire-and-forget async.** Every task is awaited or deliberately owned by something that
  observes its failure; cancellation tokens and contexts are passed all the way down.

### What is left in the file

- **Comments say why, not what.** No commented-out code; no `TODO` without a task on the board.
- **Dead code is deleted** — unused imports, parameters, private members and branches.
