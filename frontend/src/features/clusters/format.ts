/** "12 days", "5 hours", "20 minutes": how long a job has been silent, for a sentence. */
export function silentFor(seconds: number): string {
  const unit = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
  if (seconds >= 86400) return unit(Math.floor(seconds / 86400), "day");
  if (seconds >= 3600) return unit(Math.floor(seconds / 3600), "hour");
  return unit(Math.max(1, Math.floor(seconds / 60)), "minute");
}
