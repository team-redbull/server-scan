import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { getSkill, skillDownloadUrl } from "@/api/skill";
import type { CreatedToken } from "@/api/personalToken";
import type { SkillFile } from "@/api/skill";
import { queryKeys } from "@/api/queryKeys";
import { CopyButton } from "@/components/CopyButton";
import { extractPrompts, outline } from "@/features/skill/skillText";
import { McpSection } from "@/features/skill/McpSection";
import { TokenSection } from "@/features/skill/TokenSection";
import { useAuth } from "@/features/auth/useAuth";
import { copyText } from "@/lib/clipboard";
import { showToast } from "@/lib/toast";

const REFRESH_MS = 60_000;
const PRESS =
  "transition-transform duration-100 ease-out active:scale-[0.97] motion-reduce:transition-none motion-reduce:active:scale-100";
const FOCUS =
  "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--color-status-info)]";

/** A floating glass layer: translucent, blurred, with a bright top edge where light catches it. */
const GLASS =
  "rounded-2xl border border-[var(--border-subtle)] border-t-[oklch(1_0_0/0.12)] bg-[oklch(0.21_0.007_260/0.7)] shadow-[0_12px_40px_-12px_oklch(0_0_0/0.6)] backdrop-blur-xl motion-reduce:backdrop-blur-none";

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <li className="flex gap-4">
      <span
        aria-hidden="true"
        className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full bg-[var(--tint-info)] text-sm font-semibold text-[var(--text-on-info)]"
      >
        {n}
      </span>
      <div className="min-w-0 flex-1">
        <h3 className="text-[0.95rem] font-semibold tracking-tight text-[var(--text-primary)]">
          {title}
        </h3>
        <div className="mt-1.5 text-sm leading-relaxed text-[var(--text-secondary)]">{children}</div>
      </div>
    </li>
  );
}

function CommandLine({ command, label }: { command: string; label: string }) {
  return (
    <div className="mt-2 flex items-center gap-2 rounded-lg border border-[var(--border-subtle)] bg-[var(--surface-sunken)] py-1.5 pr-1.5 pl-3">
      <code className="min-w-0 flex-1 overflow-x-auto font-mono text-[0.8rem] whitespace-nowrap text-[var(--text-primary)]">
        {command}
      </code>
      <CopyButton text={command} label={label} />
    </div>
  );
}

function PromptChip({ prompt }: { prompt: string }) {
  return (
    <button
      type="button"
      onClick={() => {
        void copyText(prompt).then((ok) => {
          showToast({ title: ok ? "Copied" : "Copy failed", detail: prompt, ok });
        });
      }}
      className={`shrink-0 rounded-full border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-3 py-1 text-left text-[0.8rem] whitespace-nowrap text-[var(--text-secondary)] hover:border-[var(--border-strong)] hover:text-[var(--text-primary)] ${PRESS} ${FOCUS}`}
    >
      “{prompt}”
    </button>
  );
}

function Source({ files }: { files: SkillFile[] }) {
  const [path, setPath] = useState(files[0]!.path);
  const file = files.find((f) => f.path === path) ?? files[0]!;
  const lines = file.content.split("\n");
  const headings = outline(file.content);

  function jump(line: number) {
    const target = document.getElementById(`skill-line-${String(line)}`);
    const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    target?.scrollIntoView({ behavior: calm ? "auto" : "smooth", block: "start" });
  }

  return (
    <section aria-labelledby="skill-source" className="mt-14">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2
            id="skill-source"
            className="text-2xl leading-tight font-semibold tracking-[-0.02em] text-[var(--text-primary)]"
          >
            What’s inside
          </h2>
          <p className="mt-1 text-sm text-[var(--text-secondary)]">
            Exactly what Claude reads. This page is rendered from the files on the server.
          </p>
        </div>
        <div role="tablist" aria-label="Skill file" className="flex gap-1.5">
          {files.map((f) => (
            <button
              key={f.path}
              type="button"
              role="tab"
              aria-selected={f.path === file.path}
              onClick={() => {
                setPath(f.path);
              }}
              className={`rounded-lg px-3 py-1.5 font-mono text-xs ${PRESS} ${FOCUS} ${
                f.path === file.path
                  ? "bg-[var(--tint-info)] text-[var(--text-on-info)]"
                  : "bg-[var(--surface-raised)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
              }`}
            >
              {f.path}
            </button>
          ))}
        </div>
      </div>

      <div className={`${GLASS} mt-5 grid overflow-hidden lg:grid-cols-[14rem_1fr]`}>
        <nav
          aria-label="On this page"
          className="hidden border-r border-[var(--border-subtle)] p-4 lg:block"
        >
          <p className="mb-2 text-xs font-medium tracking-wide text-[var(--text-muted)] uppercase">
            On this page
          </p>
          <ul className="max-h-[70vh] space-y-0.5 overflow-y-auto">
            {headings.map((h) => (
              <li key={h.line}>
                <button
                  type="button"
                  onClick={() => {
                    jump(h.line);
                  }}
                  className={`w-full rounded-md px-2 py-1 text-left text-[0.8rem] text-[var(--text-secondary)] hover:bg-[var(--surface-hover)] hover:text-[var(--text-primary)] ${FOCUS} ${
                    h.level === 3 ? "pl-5" : ""
                  }`}
                >
                  {h.text}
                </button>
              </li>
            ))}
          </ul>
        </nav>

        <div className="min-w-0">
          <div className="flex items-center justify-between border-b border-[var(--border-subtle)] px-4 py-2">
            <span className="font-mono text-xs text-[var(--text-muted)]">
              {lines.length} lines
            </span>
            <CopyButton text={file.content} label={`Copy ${file.path}`} />
          </div>
          <pre
            tabIndex={0}
            aria-label={file.path}
            className={`max-h-[70vh] overflow-auto py-3 font-mono text-[0.78rem] leading-6 ${FOCUS}`}
          >
            {lines.map((line, index) => (
              // A fixed file's lines never reorder, so the index is the stable key.
              <div key={index} id={`skill-line-${String(index)}`} className="flex">
                <span
                  aria-hidden="true"
                  className="w-12 shrink-0 pr-4 text-right text-[var(--text-muted)] select-none"
                >
                  {index + 1}
                </span>
                <code
                  className={`pr-4 break-words whitespace-pre-wrap ${
                    line.startsWith("#") ? "font-semibold text-[var(--text-primary)]" : "text-[var(--text-secondary)]"
                  }`}
                >
                  {line || " "}
                </code>
              </div>
            ))}
          </pre>
        </div>
      </div>
    </section>
  );
}

/** A guide to the Claude Code skill for this API. Everything on it — the version, the example
 * prompts, the files — is read from `GET /api/v1/skill`, so editing the skill updates the page. */
export function SkillPage() {
  const { data: me } = useAuth();
  const [fresh, setFresh] = useState<CreatedToken | null>(null);
  const { data: skill, isPending, isError } = useQuery({
    queryKey: queryKeys.skill.all,
    queryFn: getSkill,
    staleTime: 0,
    refetchInterval: REFRESH_MS,
    refetchOnWindowFocus: true,
  });

  if (isPending) {
    return (
      <main className="mx-auto max-w-5xl px-8 py-16 text-sm text-[var(--text-secondary)]">
        Loading the skill…
      </main>
    );
  }
  if (isError) {
    return (
      <main className="mx-auto max-w-5xl px-8 py-16">
        <h1 className="text-xl font-semibold text-[var(--text-primary)]">Claude skill</h1>
        <p role="alert" className="mt-2 text-sm text-[var(--text-secondary)]">
          This server carries no skill, or it could not be read.
        </p>
      </main>
    );
  }

  const origin = window.location.origin;
  const tokenLine = me?.role === "VIEWER" ? "VIEWER_TOKEN" : "AUDITOR_TOKEN";
  const prompts = extractPrompts(skill.description, skill.when_to_use);
  const folder = `~/.claude/skills/${skill.name}`;

  return (
    <main className="mx-auto max-w-5xl px-8 pt-14 pb-24">
      <header>
        <h1 className="max-w-3xl text-5xl leading-[1.05] font-semibold tracking-[-0.03em] text-balance text-[var(--text-primary)]">
          Ask Claude about Server Scan
        </h1>
        <p className="mt-4 max-w-2xl text-lg leading-relaxed text-pretty text-[var(--text-secondary)]">
          Install the <span className="font-mono text-[0.95em]">{skill.name}</span> skill in Claude
          Code and ask in plain language. It builds the API calls, pages through the results and
          answers with real data.
        </p>
        <a
          href={skillDownloadUrl(origin)}
          download
          className={`mt-7 inline-flex items-center gap-2 rounded-full bg-[var(--color-status-info)] px-5 py-2.5 text-sm font-semibold text-white hover:brightness-110 ${PRESS} ${FOCUS}`}
        >
          <svg viewBox="0 0 16 16" className="size-4 fill-none stroke-current" strokeWidth="1.75" aria-hidden="true">
            <path d="M8 2v8m0 0L5 7m3 3 3-3M3 13h10" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          Download {skill.name}.zip
        </a>
      </header>

      <section aria-labelledby="skill-install" className={`${GLASS} mt-12 p-7`}>
        <h2
          id="skill-install"
          className="text-2xl leading-tight font-semibold tracking-[-0.02em] text-[var(--text-primary)]"
        >
          Set it up in a minute
        </h2>
        <ol className="mt-6 space-y-6">
          <Step n={1} title="Download the zip">
            The address of this server ({" "}
            <span className="font-mono text-[var(--text-primary)]">{origin}</span> ) is already
            written into it.
          </Step>
          <Step n={2} title="Unzip it into your skills folder">
            <CommandLine
              command={`unzip -o ~/Downloads/${skill.name}.zip -d ~/.claude/skills`}
              label="Copy the unzip command"
            />
          </Step>
          <Step n={3} title="Add your token">
            Create it below, then open{" "}
            <span className="font-mono text-[var(--text-primary)]">{folder}/SKILL.md</span> and
            replace the <span className="font-mono">{tokenLine}</span> placeholder. Keep it out of
            git.
          </Step>
          <Step n={4} title="Ask">
            Start Claude Code and just ask.
            {prompts.length > 0 && (
              <div className="mt-3 flex gap-2 overflow-x-auto pb-1">
                {prompts.map((prompt) => (
                  <PromptChip key={prompt} prompt={prompt} />
                ))}
              </div>
            )}
          </Step>
        </ol>
      </section>

      <TokenSection fresh={fresh} onFresh={setFresh} />

      <McpSection origin={origin} token={fresh?.token ?? null} />

      <section aria-labelledby="skill-about" className="mt-14 grid gap-8 md:grid-cols-2">
        <div>
          <h2
            id="skill-about"
            className="text-2xl leading-tight font-semibold tracking-[-0.02em] text-[var(--text-primary)]"
          >
            When Claude uses it
          </h2>
          <p className="mt-3 text-sm leading-relaxed text-[var(--text-secondary)]">
            {skill.description}
          </p>
          {skill.when_to_use && (
            <p className="mt-3 text-sm leading-relaxed text-[var(--text-secondary)]">
              {skill.when_to_use}
            </p>
          )}
        </div>
        <div>
          <h2 className="text-2xl leading-tight font-semibold tracking-[-0.02em] text-[var(--text-primary)]">
            What it may run
          </h2>
          <p className="mt-3 text-sm leading-relaxed text-[var(--text-secondary)]">
            Read-only. The skill is limited to these tools and stays on GET requests.
          </p>
          <p className="mt-3 flex flex-wrap gap-2">
            {skill.allowed_tools.split(/\s+(?=\w+\()/).filter(Boolean).map((tool) => (
              <span
                key={tool}
                className="rounded-md border border-[var(--border-subtle)] bg-[var(--surface-raised)] px-2 py-1 font-mono text-xs text-[var(--text-secondary)]"
              >
                {tool}
              </span>
            ))}
          </p>
        </div>
      </section>

      {skill.files.length > 0 && <Source files={skill.files} />}
    </main>
  );
}
