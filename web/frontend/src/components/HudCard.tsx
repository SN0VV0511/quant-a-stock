import type { ReactNode } from "react";
import { motion } from "framer-motion";

interface HudCardProps {
  title?: string;
  meta?: ReactNode;
  icon?: ReactNode;
  children: ReactNode;
  className?: string;
  compact?: boolean;
}

export function HudCard({ title, meta, icon, children, className = "", compact = false }: HudCardProps) {
  return (
    <motion.section
      className={`hud-card ${compact ? "hud-card--compact" : ""} ${className}`}
      initial={{ opacity: 0, y: 16 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.28, ease: "easeOut" }}
    >
      {(title || meta) && (
        <header className="hud-card__head">
          <div className="hud-card__title">
            {icon}
            <span>{title}</span>
          </div>
          <div className="hud-card__meta">{meta}</div>
        </header>
      )}
      <div className="hud-card__body">{children}</div>
    </motion.section>
  );
}
