/**
 * FR25 offline field capture: drafts live in IndexedDB under client UUIDs until synced.
 *
 * The local cache holds sensitive field data, so it carries an explicit expiry: the server returns
 * `cache_expires_at` on registration and on every sync, and once it has passed every draft and the
 * device secret are deleted on the next open, synced or not. A revoked device gets DEVICE_REVOKED from
 * the server and the cache is wiped at once.
 */

export interface LocalDraft {
  id: string;
  pid: string;
  data: Record<string, string>;
  client_revision: number;
  client_updated_at: string;
  status: "unsynced" | "pending" | "conflict" | "accepted" | "superseded";
  dirty: boolean;
}

export interface DeviceCreds { id: string; secret: string; name: string; cache_expires_at: string }

const DB = "ajgem-field";
const DRAFTS = "drafts";
const META = "meta";

/** Pure: whether the cache has passed its expiry (missing expiry counts as expired). */
export function isExpired(expiresAt: string | null | undefined, now: Date = new Date()): boolean {
  if (!expiresAt) return true;
  const t = Date.parse(expiresAt);
  return Number.isNaN(t) || t <= now.getTime();
}

/** Pure: bump a draft after a local edit, keeping its id so the server sees an update, not a new draft. */
export function editDraft(d: LocalDraft, data: Record<string, string>, now: Date = new Date()): LocalDraft {
  return { ...d, data, client_revision: d.client_revision + 1, client_updated_at: now.toISOString(), dirty: true };
}

export function newDraft(pid: string, data: Record<string, string>, now: Date = new Date()): LocalDraft {
  return { id: crypto.randomUUID(), pid, data, client_revision: 1, client_updated_at: now.toISOString(),
           status: "unsynced", dirty: true };
}

function open(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const r = indexedDB.open(DB, 1);
    r.onupgradeneeded = () => {
      r.result.createObjectStore(DRAFTS, { keyPath: "id" });
      r.result.createObjectStore(META);
    };
    r.onsuccess = () => resolve(r.result);
    r.onerror = () => reject(r.error);
  });
}

async function tx<T>(store: string, mode: IDBTransactionMode, fn: (s: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await open();
  return new Promise((resolve, reject) => {
    const t = db.transaction(store, mode);
    const req = fn(t.objectStore(store));
    t.oncomplete = () => { db.close(); resolve(req.result); };
    t.onerror = () => { db.close(); reject(t.error); };
  });
}

export const store = {
  device: () => tx<DeviceCreds | undefined>(META, "readonly", (s) => s.get("device") as IDBRequest<DeviceCreds | undefined>),
  setDevice: (d: DeviceCreds) => tx(META, "readwrite", (s) => s.put(d, "device")),
  drafts: async (pid: string) =>
    (await tx<LocalDraft[]>(DRAFTS, "readonly", (s) => s.getAll() as IDBRequest<LocalDraft[]>)).filter((d) => d.pid === pid),
  put: (d: LocalDraft) => tx(DRAFTS, "readwrite", (s) => s.put(d)),
  async wipe(): Promise<void> {
    await tx(DRAFTS, "readwrite", (s) => s.clear());
    await tx(META, "readwrite", (s) => s.clear());
  },
  /** Deletes everything when the cache has expired; returns whether it did. */
  async enforceExpiry(now: Date = new Date()): Promise<boolean> {
    const d = await store.device();
    if (d && isExpired(d.cache_expires_at, now)) {
      await store.wipe();
      return true;
    }
    return false;
  },
};
