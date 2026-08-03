// PREDICT — WebSocket provider with a subscribe API.
// Pages register handlers per event type; each message is delivered once.
// (This replaces the old growing-array pattern that stalled after 100 messages.)
import {
  createContext, useCallback, useContext, useEffect, useRef, useState,
  type ReactNode,
} from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "./auth";
import type { WsEvent } from "./types";
import { qk } from "./api";

type Handler = (data: any) => void;

interface WsContextValue {
  connected: boolean;
  subscribe: (type: WsEvent["type"], handler: Handler) => () => void;
}

const WsContext = createContext<WsContextValue>({
  connected: false,
  subscribe: () => () => {},
});

export const useWs = () => useContext(WsContext);

export function WsProvider({ children }: { children: ReactNode }) {
  const [connected, setConnected] = useState(false);
  const handlers = useRef(new Map<string, Set<Handler>>());
  const queryClient = useQueryClient();
  const { authenticated } = useAuth();

  const subscribe = useCallback((type: string, handler: Handler) => {
    let set = handlers.current.get(type);
    if (!set) {
      set = new Set();
      handlers.current.set(type, set);
    }
    set.add(handler);
    return () => {
      set!.delete(handler);
    };
  }, []);

  useEffect(() => {
    // Don't connect until the user is authenticated (or auth is disabled).
    if (!authenticated) return;

    let ws: WebSocket | null = null;
    let closed = false;
    let retry: ReturnType<typeof setTimeout>;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        if (!closed) retry = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws?.close();
      ws.onmessage = (ev) => {
        let msg: WsEvent;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        // Targeted query invalidation keeps REST data fresh without polling storms.
        switch (msg.type) {
          case "alert":
          case "alert_resolved":
            queryClient.invalidateQueries({ queryKey: ["alerts"] });
            queryClient.invalidateQueries({ queryKey: qk.summary });
            queryClient.invalidateQueries({ queryKey: qk.overview });
            break;
          case "work_order":
            queryClient.invalidateQueries({ queryKey: ["workorders"] });
            queryClient.invalidateQueries({ queryKey: qk.summary });
            queryClient.invalidateQueries({ queryKey: qk.history });
            queryClient.invalidateQueries({ queryKey: qk.overview });
            break;
          case "health":
          case "trip":
          case "driving_event":
            queryClient.invalidateQueries({ queryKey: qk.driving });
            break;
          case "settings":
            queryClient.invalidateQueries({ queryKey: qk.settings });
            break;
        }
        handlers.current.get(msg.type)?.forEach((h) => {
          try {
            h((msg as any).data);
          } catch (e) {
            console.error("WS handler error", e);
          }
        });
      };
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(retry);
      ws?.close();
    };
  }, [authenticated, queryClient]);

  return (
    <WsContext.Provider value={{ connected, subscribe }}>
      {children}
    </WsContext.Provider>
  );
}
