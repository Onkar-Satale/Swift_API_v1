import { ApiError } from '../utils/ApiError.js';

/**
 * Global Express error handling middleware.
 * Transforms database (Mongoose CastError, Duplicate key 11000, ValidationError)
 * and generic errors into standardized ApiError formats, logs them, and formats JSON responses.
 */
const errorHandler = (err, req, res, next) => {
  let error = err;

  if (err.name === "CastError") {
    const message = `Resource not found. Invalid: ${err.path}`;
    error = new ApiError(404, message);
  }

  if (err.code === 11000) {
    const field = Object.keys(err.keyValue)[0];
    const message = `An account with that ${field} already exists. Please use a different one.`;
    error = new ApiError(400, message);
  }

  if (err.name === "ValidationError") {
    const message = Object.values(err.errors).map((val) => val.message).join(", ");
    error = new ApiError(400, message);
  }

  if (!(error instanceof ApiError)) {
    const statusCode = error.statusCode || 500;
    const message = error.message || "Internal Server Error";
    error = new ApiError(statusCode, message, false, err.stack);
  }

  const response = {
    success: false,
    message: error.message,
    ...(process.env.NODE_ENV === "development" && { stack: error.stack }),
  };

  if (error.statusCode >= 500) {
    console.error(`[ERROR ${error.statusCode}] ${req.method} ${req.originalUrl}:`, error.stack || error.message);
  } else {
    console.warn(`[WARN ${error.statusCode}] ${req.method} ${req.originalUrl}: ${error.message}`);
  }

  res.status(error.statusCode).json(response);
};

export default errorHandler;