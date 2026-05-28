import type { Metadata } from "next";
import { Geist_Mono } from "next/font/google";
import "./globals.css";
import Nav from "@/components/Nav";
import Providers from "@/components/Providers";

const mono = Geist_Mono({ subsets: ["latin"], variable: "--font-mono" });

export const metadata: Metadata = {
  title: "Meta-Analyst",
  description: "Quantitative trading signals dashboard",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className={`${mono.variable} font-mono bg-white dark:bg-zinc-950 text-zinc-900 dark:text-zinc-100 min-h-screen transition-colors`}>
        <Providers>
          <Nav />
          <main className="w-full px-4 sm:px-6 lg:px-10 py-6">{children}</main>
        </Providers>
      </body>
    </html>
  );
}
