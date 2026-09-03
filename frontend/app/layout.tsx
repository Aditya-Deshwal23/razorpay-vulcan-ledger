import "@fontsource/fraunces/500.css";
import "@fontsource/fraunces/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";

import type { Metadata } from "next";
import { Suspense } from "react";

import { AppShell } from "@/components/app-shell";
import "./globals.css";

export const metadata: Metadata = {
  title: "Vulcan Ledger",
  description: "Audit-grade Razorpay settlement reconciliation.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body><Suspense fallback={<main className="shell-loading" />}> <AppShell>{children}</AppShell></Suspense></body>
    </html>
  );
}
