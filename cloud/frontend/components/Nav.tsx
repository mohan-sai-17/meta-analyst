"use client";
import { useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Menu, X } from "lucide-react";
import ThemeToggle from "@/components/ThemeToggle";

const links = [
  { href: "/",              label: "Signals" },
  { href: "/oracle",        label: "Oracle" },
  { href: "/weatherman",    label: "Weatherman" },
  { href: "/top-picks",     label: "Top Picks" },
  { href: "/sector",        label: "Sector" },
  { href: "/scanner",       label: "Scanner" },
  { href: "/fundamentals",  label: "Fundamentals" },
  { href: "/backtest",      label: "Backtest" },
  { href: "/compounder",    label: "Compounder" },
  { href: "/wfo",           label: "WFO" },
  { href: "/ml-signals",    label: "ML Filter" },
  { href: "/combo",         label: "Combo" },
  { href: "/ask",           label: "Ask AI" },
];

export default function Nav() {
  const path = usePathname();
  const [open, setOpen] = useState(false);

  return (
    <nav className="border-b border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-950 sticky top-0 z-50">
      {/* Top bar */}
      <div className="w-full px-4 sm:px-6 lg:px-10 flex items-center h-14">
        {/* Logo */}
        <span className="text-green-500 dark:text-green-400 font-bold tracking-tight mr-6 shrink-0">
          META-ANALYST
        </span>

        {/* Desktop links */}
        <div className="hidden md:flex items-center gap-1 flex-1">
          {links.map(({ href, label }) => {
            const active = href === "/" ? path === "/" : path.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                className={`px-3 py-1.5 rounded text-sm transition-colors ${
                  active
                    ? "bg-zinc-100 dark:bg-zinc-800 text-zinc-900 dark:text-zinc-100"
                    : "text-zinc-500 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-100 hover:bg-zinc-100 dark:hover:bg-zinc-900"
                }`}
              >
                {label}
              </Link>
            );
          })}
        </div>

        {/* Right side: ThemeToggle always visible, hamburger on mobile */}
        <div className="ml-auto flex items-center gap-2">
          <ThemeToggle />
          <button
            onClick={() => setOpen(o => !o)}
            className="md:hidden p-1.5 rounded text-zinc-500 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-100 hover:bg-zinc-100 dark:hover:bg-zinc-900 transition-colors"
            aria-label="Toggle menu"
          >
            {open ? <X size={20} /> : <Menu size={20} />}
          </button>
        </div>
      </div>

      {/* Mobile dropdown */}
      {open && (
        <div className="md:hidden border-t border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-950 px-4 py-2 flex flex-col gap-1">
          {links.map(({ href, label }) => {
            const active = href === "/" ? path === "/" : path.startsWith(href);
            return (
              <Link
                key={href}
                href={href}
                onClick={() => setOpen(false)}
                className={`px-3 py-2 rounded text-sm transition-colors ${
                  active
                    ? "bg-zinc-100 dark:bg-zinc-800 text-zinc-900 dark:text-zinc-100"
                    : "text-zinc-500 dark:text-zinc-400 hover:text-zinc-900 dark:hover:text-zinc-100 hover:bg-zinc-100 dark:hover:bg-zinc-900"
                }`}
              >
                {label}
              </Link>
            );
          })}
        </div>
      )}
    </nav>
  );
}
