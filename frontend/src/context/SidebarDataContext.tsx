import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useLocation } from "react-router-dom";

import {
  aiChat,
  connections as connApi,
  type AIConversationSummary,
} from "@/api/endpoints";
import { useAuth } from "@/context/useAuth";
import type { Connection } from "@/types";

interface SidebarDataContextValue {
  connections: Connection[];
  loadingConnections: boolean;
  refreshConnections: () => Promise<Connection[]>;
  acceptConnection: (id: string) => Promise<void>;
  rejectConnection: (id: string) => Promise<void>;

  aiSessions: AIConversationSummary[];
  loadingAiSessions: boolean;
  refreshAiSessions: () => Promise<AIConversationSummary[]>;
  deleteAiSession: (id: string) => Promise<void>;

  messagesOpen: boolean;
  setMessagesOpen: React.Dispatch<React.SetStateAction<boolean>>;
  aiOpen: boolean;
  setAiOpen: React.Dispatch<React.SetStateAction<boolean>>;
}

const SidebarDataContext = createContext<SidebarDataContextValue | null>(null);

export function SidebarDataProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const location = useLocation();

  const [connections, setConnections] = useState<Connection[]>([]);
  const [loadingConnections, setLoadingConnections] = useState<boolean>(false);

  const [aiSessions, setAiSessions] = useState<AIConversationSummary[]>([]);
  const [loadingAiSessions, setLoadingAiSessions] = useState<boolean>(false);

  const isMessagesPage = location.pathname.startsWith("/messages");
  const isAiPage = location.pathname.startsWith("/ai-chat");

  const [messagesOpen, setMessagesOpen] = useState<boolean>(isMessagesPage);
  const [aiOpen, setAiOpen] = useState<boolean>(isAiPage);

  // Auto-expand when navigating directly to the page
  useEffect(() => {
    if (isMessagesPage) {
      setMessagesOpen(true);
    }
  }, [isMessagesPage]);

  useEffect(() => {
    if (isAiPage) {
      setAiOpen(true);
    }
  }, [isAiPage]);

  const refreshConnections = useCallback(async (): Promise<Connection[]> => {
    if (!user) {
      setConnections([]);
      return [];
    }
    setLoadingConnections(true);
    try {
      const data = await connApi.listAll();
      setConnections(data);
      return data;
    } catch (err) {
      console.error("Failed to load connections:", err);
      return [];
    } finally {
      setLoadingConnections(false);
    }
  }, [user]);

  const acceptConnection = useCallback(
    async (id: string) => {
      try {
        const updated = await connApi.accept(id);
        setConnections((prev) => prev.map((c) => (c.id === id ? updated : c)));
      } catch (err) {
        console.error("Failed to accept connection:", err);
        throw err;
      }
    },
    [],
  );

  const rejectConnection = useCallback(
    async (id: string) => {
      try {
        const updated = await connApi.reject(id);
        setConnections((prev) => prev.map((c) => (c.id === id ? updated : c)));
      } catch (err) {
        console.error("Failed to reject connection:", err);
        throw err;
      }
    },
    [],
  );

  const refreshAiSessions = useCallback(async (): Promise<AIConversationSummary[]> => {
    if (!user) {
      setAiSessions([]);
      return [];
    }
    setLoadingAiSessions(true);
    try {
      const data = await aiChat.listConversations();
      setAiSessions(data);
      return data;
    } catch (err) {
      console.error("Failed to load AI conversations:", err);
      return [];
    } finally {
      setLoadingAiSessions(false);
    }
  }, [user]);

  const deleteAiSession = useCallback(
    async (id: string) => {
      try {
        await aiChat.deleteConversation(id);
        setAiSessions((prev) => prev.filter((s) => s.id !== id));
      } catch (err) {
        console.error("Failed to delete AI session:", err);
        throw err;
      }
    },
    [],
  );

  // Initial load when user signs in
  useEffect(() => {
    if (user) {
      void refreshConnections();
      void refreshAiSessions();
    } else {
      setConnections([]);
      setAiSessions([]);
    }
  }, [user, refreshConnections, refreshAiSessions]);

  const value = useMemo(
    () => ({
      connections,
      loadingConnections,
      refreshConnections,
      acceptConnection,
      rejectConnection,
      aiSessions,
      loadingAiSessions,
      refreshAiSessions,
      deleteAiSession,
      messagesOpen,
      setMessagesOpen,
      aiOpen,
      setAiOpen,
    }),
    [
      connections,
      loadingConnections,
      refreshConnections,
      acceptConnection,
      rejectConnection,
      aiSessions,
      loadingAiSessions,
      refreshAiSessions,
      deleteAiSession,
      messagesOpen,
      aiOpen,
    ],
  );

  return (
    <SidebarDataContext.Provider value={value}>
      {children}
    </SidebarDataContext.Provider>
  );
}

export function useSidebarData() {
  const ctx = useContext(SidebarDataContext);
  if (!ctx) {
    throw new Error("useSidebarData must be used within a SidebarDataProvider");
  }
  return ctx;
}
