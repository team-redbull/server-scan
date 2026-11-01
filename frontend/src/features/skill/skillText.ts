/** Example questions the skill's own description quotes: `"all Dell servers in tlv"` → a prompt chip. */
export function extractPrompts(...texts: string[]): string[] {
  const found = new Set<string>();
  for (const text of texts) {
    for (const match of text.matchAll(/"([^"]{8,80})"/g)) {
      const prompt = match[1]!.trim();
      found.add(prompt.charAt(0).toUpperCase() + prompt.slice(1));
    }
  }
  return [...found].slice(0, 6);
}

export interface Heading {
  level: 2 | 3;
  text: string;
  line: number;
}

/** The `##`/`###` headings of a markdown file, skipping fenced code and the frontmatter. */
export function outline(content: string): Heading[] {
  const headings: Heading[] = [];
  let fenced = false;
  content.split("\n").forEach((raw, index) => {
    if (raw.startsWith("```")) {
      fenced = !fenced;
      return;
    }
    const match = !fenced ? /^(#{2,3})\s+(.+?)\s*$/.exec(raw) : null;
    if (match) {
      headings.push({ level: match[1]!.length === 2 ? 2 : 3, text: match[2]!, line: index });
    }
  });
  return headings;
}
