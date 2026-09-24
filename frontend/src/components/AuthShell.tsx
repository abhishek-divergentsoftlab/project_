import type { ReactNode } from "react";

import { IconBrand } from "@/components/icons";
import { ThemeToggle } from "@/components/ThemeToggle";

export function AuthShell({ children }: { children: ReactNode }) {
  return (
    <div className="auth-shell">
      <header className="auth-shell-top">
        <div className="auth-brand">
          <IconBrand size={24} />
          <span>Marketplace</span>
        </div>
        <ThemeToggle compact />
      </header>
      <div className="auth-shell-body">{children}</div>
    </div>
  );
}
