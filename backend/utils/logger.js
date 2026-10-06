/**
 * Winston Logging Module
 * Formatted console logging for server operations, requests, and errors.
 */

import winston from 'winston';

const logger = winston.createLogger({
  level: process.env.NODE_ENV === "production" ? "info" : "debug",
  format: winston.format.combine(
    winston.format.errors({ stack: true }),
    winston.format.splat(),
    winston.format.timestamp({ format: "YYYY-MM-DD HH:mm:ss" }),
    winston.format.colorize(),
    winston.format.printf(({ level, message, timestamp, stack }) => {
      if (stack) {
        return `[${timestamp}] ${level}: ${message}\n${stack}`;
      }
      return `[${timestamp}] ${level}: ${message}`;
    })
  ),
  defaultMeta: { service: "swift-api" },
  transports: [
    new winston.transports.Console()
  ],
});

export default logger;

