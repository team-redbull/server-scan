import type { StorageDrive } from "@/types/server";

/** Drives smallest first, biggest last. A drive whose capacity was not read
 * sorts after every known one rather than floating to the top as a zero, and
 * equal capacities keep the order the collector reported them in (the sort is
 * stable). Returns a copy; the caller's array is left alone. */
export function sortDrivesByCapacity(drives: readonly StorageDrive[]): StorageDrive[] {
  return [...drives].sort((a, b) => {
    if (a.capacity_bytes === null && b.capacity_bytes === null) {
      return 0;
    }
    if (a.capacity_bytes === null) {
      return 1;
    }
    if (b.capacity_bytes === null) {
      return -1;
    }
    return a.capacity_bytes - b.capacity_bytes;
  });
}
