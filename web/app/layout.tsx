import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "NSE Momentum Scanner",
  description: "Daily NSE-equity momentum scan with trailing-stop analysis",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <header className="border-b border-slate-800 px-6 py-4">
          <a href="/" className="text-lg font-semibold tracking-tight">
            NSE Momentum Scanner
          </a>
        </header>
        <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
