/* ==========================================================================
   pvecenter — 虚拟机 / 容器的「置顶」与「收藏（星标）」
   ==========================================================================

   为什么存服务端（`/api/prefs`，key = `guest_pins`）而不是 localStorage
   ------------------------------------------------------------------
   列显隐那套（ColumnSettings）存本地是对的：它回答的是「这台设备屏幕够不够
   宽」。置顶与收藏回答的是「我在意哪几台机器」，属于账号级认知 —— 换台电脑
   打开、换个浏览器登录都该还在。项目已有 prefs 基础设施与白名单校验，直接
   复用，不必再发明一套本地格式。

   为什么 pinned 是有序数组、starred 只是集合
   ----------------------------------------
   置顶要按用户建立的先后顺序展示，顺序本身就是信息（先置顶的排在前）；
   收藏只回答「是不是重点」，不需要次序。

   机器标识统一用 `connection_id:node/vmid`
   -----------------------------------
   与列表里的 rowKeyOf 同一个格式。面板可能接多套 PVE，同一个 vmid 在两边
   撞车是常态，只用 vmid 会让「置顶 100 号机」在两套集群上同时生效。
   ========================================================================== */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { prefsApi } from '../api/endpoints';
import { errorMessage } from '../api/client';
import { useToast } from './useToast';
import { useT } from '../i18n';

/** 偏好在服务端的键名，必须与后端 `prefs.ALLOWED_KEYS` 一致。 */
export const GUEST_PINS_KEY = 'guest_pins';

export interface GuestPins {
  /** 有序：顺序即置顶顺序 */
  pinned: string[];
  /** 无序：只需要判断在不在里面 */
  starred: string[];
}

const EMPTY: GuestPins = { pinned: [], starred: [] };

/**
 * 把服务端返回的任意结构收敛成可用形状。
 *
 * 后端只校验结构、不校验机器是否还存在（机器会被删、会迁移），所以这里不做
 * 存在性对账 —— 认不出的键在列表里自然不会命中，等用户下次保存时就顺带清掉了。
 */
function normalize(raw: unknown): GuestPins {
  if (!raw || typeof raw !== 'object') return EMPTY;
  const obj = raw as { pinned?: unknown; starred?: unknown };
  const list = (value: unknown): string[] =>
    Array.isArray(value)
      ? value.filter((item): item is string => typeof item === 'string' && item.length > 0)
      : [];
  return {
    pinned: [...new Set(list(obj.pinned))],
    starred: [...new Set(list(obj.starred))],
  };
}

export interface GuestPinsApi {
  pinned: string[];
  starred: string[];
  pinnedSet: Set<string>;
  starredSet: Set<string>;
  /** 首次读取是否已结束（读失败也算结束，按空偏好继续） */
  ready: boolean;
  isPinned: (ref: string) => boolean;
  isStarred: (ref: string) => boolean;
  togglePin: (ref: string) => void;
  toggleStar: (ref: string) => void;
}

export function useGuestPins(): GuestPinsApi {
  const t = useT();
  const toast = useToast();
  const [state, setState] = useState<GuestPins>(EMPTY);
  const [ready, setReady] = useState(false);

  /* 最新值放 ref：写回时直接读 state 会拿到本次渲染的旧快照
     （setState 是异步的，连点两下会丢掉第一下的结果）。 */
  const latest = useRef<GuestPins>(EMPTY);

  const apply = useCallback((next: GuestPins) => {
    latest.current = next;
    setState(next);
  }, []);

  const load = useCallback(async () => {
    try {
      const payload = await prefsApi.list();
      apply(normalize(payload.prefs?.[GUEST_PINS_KEY]));
    } catch {
      /* 读不到偏好不该让列表打不开 —— 按「什么都没收藏」继续 */
    } finally {
      setReady(true);
    }
  }, [apply]);

  useEffect(() => {
    void load();
  }, [load]);

  /* 写回串行化：连点两下星标会并发两个 PUT，到达顺序不保证，后发的旧值可能
     覆盖先发的新值。这里把目标值压进 pending，一个写完再写下一个，始终只保留
     最后一次的目标值。 */
  const pending = useRef<GuestPins | null>(null);
  const saving = useRef(false);

  const flush = useCallback(async () => {
    if (saving.current) return;
    saving.current = true;
    try {
      while (pending.current) {
        const payload = pending.current;
        pending.current = null;
        await prefsApi.save(GUEST_PINS_KEY, payload);
      }
    } catch (err) {
      pending.current = null;
      toast.error(t('guestList.pinSaveFailed'), errorMessage(err));
      /* 写失败就把界面拉回服务端的真实值 —— 不能让「点了但没存上」停留在屏幕上 */
      await load();
    } finally {
      saving.current = false;
    }
  }, [load, t, toast]);

  const persist = useCallback(
    (next: GuestPins) => {
      apply(next);
      pending.current = next;
      void flush();
    },
    [apply, flush],
  );

  const togglePin = useCallback(
    (ref: string) => {
      const current = latest.current;
      const next = current.pinned.includes(ref)
        ? current.pinned.filter((item) => item !== ref)
        : [...current.pinned, ref];
      persist({ ...current, pinned: next });
    },
    [persist],
  );

  const toggleStar = useCallback(
    (ref: string) => {
      const current = latest.current;
      const next = current.starred.includes(ref)
        ? current.starred.filter((item) => item !== ref)
        : [...current.starred, ref];
      persist({ ...current, starred: next });
    },
    [persist],
  );

  const pinnedSet = useMemo(() => new Set(state.pinned), [state.pinned]);
  const starredSet = useMemo(() => new Set(state.starred), [state.starred]);

  const isPinned = useCallback((ref: string) => pinnedSet.has(ref), [pinnedSet]);
  const isStarred = useCallback((ref: string) => starredSet.has(ref), [starredSet]);

  return {
    pinned: state.pinned,
    starred: state.starred,
    pinnedSet,
    starredSet,
    ready,
    isPinned,
    isStarred,
    togglePin,
    toggleStar,
  };
}
