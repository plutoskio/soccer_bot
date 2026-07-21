import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Soccer Bot — Match probabilities",
  description: "Professional soccer forecasts with transparent model evidence.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en" data-scroll-behavior="smooth"><body>{children}</body></html>;
}
