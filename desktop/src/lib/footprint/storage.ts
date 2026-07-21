// Footprint 历史柱 IndexedDB 持久化 v2（原生 API，无第三方依赖）。
//
// 存储结构：db `jarvis-footprint` / store `bars`，复合主键
// [symbol, timeframe, time]，同柱重复写入即覆盖（upsert），天然满足
// 「同柱更新以 time 相同判定」的契约。
// v2 变更：主键从 [timeframe, time] 扩为 [symbol, timeframe, time]，
// DB_VERSION 升到 2，onupgradeneeded 直接删旧 store 重建（v1 只有 mock
// 数据，无迁移价值）；新增 clearSymbol 支持按币种重建。
//
// 运行环境：Electron renderer / 浏览器。vitest node 环境无 indexedDB，
// 此时自动降级为进程内 Map 存储（接口行为一致，仅不持久化）。

import type { FootprintBar, Timeframe } from '../../types/footprint';
import { TIMEFRAMES } from './aggregator';

/** 默认库名（mock 源）；真实源传 jarvis-footprint-real 隔离 */
const DEFAULT_DB_NAME = 'jarvis-footprint';
const DB_VERSION = 2;
const STORE = 'bars';

export interface FootprintStorage {
  putBars(bars: readonly FootprintBar[]): Promise<void>;
  getBars(symbol: string, timeframe: Timeframe, from: number, to: number): Promise<FootprintBar[]>;
  /** 某 (symbol, timeframe) 已存柱数（诊断用） */
  count(symbol: string, timeframe: Timeframe): Promise<number>;
  /** 删除某 symbol 的全部周期数据（重建该币种时用） */
  clearSymbol(symbol: string): Promise<void>;
  clear(): Promise<void>;
}

// ---------------------------------------------------------------- IndexedDB

class IdbFootprintStorage implements FootprintStorage {
  private dbPromise: Promise<IDBDatabase> | null = null;

  constructor(private readonly dbName: string) {}

  private open(): Promise<IDBDatabase> {
    if (!this.dbPromise) {
      this.dbPromise = new Promise((resolve, reject) => {
        const req = indexedDB.open(this.dbName, DB_VERSION);
        req.onupgradeneeded = () => {
          const db = req.result;
          // v1→v2 主键变更：直接重建（mock 数据可丢弃，无迁移）
          if (db.objectStoreNames.contains(STORE)) db.deleteObjectStore(STORE);
          db.createObjectStore(STORE, { keyPath: ['symbol', 'timeframe', 'time'] });
        };
        req.onsuccess = () => {
          const db = req.result;
          // 连接被更高版本挤掉时重置，下次调用重新 open
          db.onversionchange = () => {
            db.close();
            this.dbPromise = null;
          };
          resolve(db);
        };
        req.onerror = () => reject(req.error ?? new Error('indexedDB open failed'));
      });
      this.dbPromise.catch(() => {
        this.dbPromise = null;
      });
    }
    return this.dbPromise;
  }

  private async tx(mode: IDBTransactionMode): Promise<IDBObjectStore> {
    const db = await this.open();
    return db.transaction(STORE, mode).objectStore(STORE);
  }

  async putBars(bars: readonly FootprintBar[]): Promise<void> {
    if (bars.length === 0) return;
    const store = await this.tx('readwrite');
    await new Promise<void>((resolve, reject) => {
      const t = store.transaction;
      for (const bar of bars) store.put(bar);
      t.oncomplete = () => resolve();
      t.onerror = () => reject(t.error ?? new Error('putBars failed'));
      t.onabort = () => reject(t.error ?? new Error('putBars aborted'));
    });
  }

  async getBars(
    symbol: string,
    timeframe: Timeframe,
    from: number,
    to: number,
  ): Promise<FootprintBar[]> {
    const store = await this.tx('readonly');
    const range = IDBKeyRange.bound([symbol, timeframe, from], [symbol, timeframe, to]);
    return new Promise((resolve, reject) => {
      const req = store.getAll(range);
      req.onsuccess = () => {
        const bars = (req.result as FootprintBar[]).sort((a, b) => a.time - b.time);
        resolve(bars);
      };
      req.onerror = () => reject(req.error ?? new Error('getBars failed'));
    });
  }

  async count(symbol: string, timeframe: Timeframe): Promise<number> {
    const store = await this.tx('readonly');
    const range = IDBKeyRange.bound(
      [symbol, timeframe, 0],
      [symbol, timeframe, Number.MAX_SAFE_INTEGER],
    );
    return new Promise((resolve, reject) => {
      const req = store.count(range);
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error ?? new Error('count failed'));
    });
  }

  async clearSymbol(symbol: string): Promise<void> {
    const store = await this.tx('readwrite');
    await new Promise<void>((resolve, reject) => {
      const t = store.transaction;
      for (const tf of TIMEFRAMES) {
        store.delete(IDBKeyRange.bound([symbol, tf, 0], [symbol, tf, Number.MAX_SAFE_INTEGER]));
      }
      t.oncomplete = () => resolve();
      t.onerror = () => reject(t.error ?? new Error('clearSymbol failed'));
      t.onabort = () => reject(t.error ?? new Error('clearSymbol aborted'));
    });
  }

  async clear(): Promise<void> {
    const store = await this.tx('readwrite');
    await new Promise<void>((resolve, reject) => {
      const req = store.clear();
      req.onsuccess = () => resolve();
      req.onerror = () => reject(req.error ?? new Error('clear failed'));
    });
  }
}

// ------------------------------------------------- in-memory fallback (test)

class MemoryFootprintStorage implements FootprintStorage {
  private readonly map = new Map<string, FootprintBar>();

  async putBars(bars: readonly FootprintBar[]): Promise<void> {
    for (const bar of bars) this.map.set(`${bar.symbol}:${bar.timeframe}:${bar.time}`, bar);
  }

  async getBars(
    symbol: string,
    timeframe: Timeframe,
    from: number,
    to: number,
  ): Promise<FootprintBar[]> {
    const out: FootprintBar[] = [];
    for (const bar of this.map.values()) {
      if (bar.symbol === symbol && bar.timeframe === timeframe && bar.time >= from && bar.time <= to) {
        out.push(bar);
      }
    }
    return out.sort((a, b) => a.time - b.time);
  }

  async count(symbol: string, timeframe: Timeframe): Promise<number> {
    let n = 0;
    for (const bar of this.map.values()) {
      if (bar.symbol === symbol && bar.timeframe === timeframe) n += 1;
    }
    return n;
  }

  async clearSymbol(symbol: string): Promise<void> {
    for (const [key, bar] of this.map) {
      if (bar.symbol === symbol) this.map.delete(key);
    }
  }

  async clear(): Promise<void> {
    this.map.clear();
  }
}

/** 环境探测：Electron renderer / 浏览器走 IndexedDB，node（vitest）降级内存 */
export function createFootprintStorage(dbName: string = DEFAULT_DB_NAME): FootprintStorage {
  if (typeof indexedDB !== 'undefined') return new IdbFootprintStorage(dbName);
  return new MemoryFootprintStorage();
}
