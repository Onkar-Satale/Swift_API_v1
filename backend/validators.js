import { body, param, validationResult } from 'express-validator';
import { ApiError } from './utils/ApiError.js';

// Evaluates express-validator chains and forwards 400 ApiError if any rule fails
export const validateRequest = (req, res, next) => {
  const errors = validationResult(req);
  if (!errors.isEmpty()) {
    const errorMsg = errors.array().map((err) => err.msg).join(', ');
    return next(new ApiError(400, `Validation Error: ${errorMsg}`));
  }
  next();
};

// --- Proxy Request Validation ---
export const requestProxyValidator = [
  body('url')
    .trim()
    .notEmpty()
    .withMessage('URL is required')
    .custom((val) => {
      try {
        let clean = String(val).trim();
        if (clean.match(/^(GET|POST|PUT|DELETE|PATCH)\s+/i)) {
          clean = clean.replace(/^(GET|POST|PUT|DELETE|PATCH)\s+/i, '');
        }
        const u = new URL(clean);
        return u.protocol === 'http:' || u.protocol === 'https:';
      } catch {
        return false;
      }
    })
    .withMessage('Must be a valid URL'),
  body('method').trim().notEmpty().withMessage('Method is required'),
  body('headers').optional(),
  body('params').optional(),
  body('body').optional(),
  validateRequest
];

// --- Auth Validation ---
export const registerValidator = [
  body('firstName').trim().notEmpty().withMessage('First name is required').isLength({ max: 50 }).withMessage('First name must be at most 50 characters long'),
  body('lastName').optional().trim().isLength({ max: 50 }).withMessage('Last name must be at most 50 characters long'),
  body('email').trim().isEmail().withMessage('Invalid email format'),
  body('password')
    .isLength({ min: 8 }).withMessage('Password must be at least 8 characters long')
    .matches(/[A-Z]/).withMessage('Password must contain at least one uppercase letter')
    .matches(/[a-z]/).withMessage('Password must contain at least one lowercase letter')
    .matches(/[0-9]/).withMessage('Password must contain at least one number'),
  validateRequest,
];

export const loginValidator = [
  body('email').trim().isEmail().withMessage('Invalid email format'),
  body('password').notEmpty().withMessage('Password is required'),
  validateRequest,
];

// --- History Validation ---
export const deleteHistoryValidator = [
  param('historyId').trim().notEmpty().withMessage('History ID is required'),
  validateRequest
];

// --- AI Validation ---
export const botValidator = [
  body('message').trim().notEmpty().withMessage('Message is required'),
  body('currentApiContext').optional(),
  body('requestHistory').optional().isArray().withMessage('Request history must be an array'),
  body('userId').optional(),
  validateRequest,
];

export const failureAssistValidator = [
  body('url').trim().notEmpty().withMessage('URL is required'),
  body('method').trim().notEmpty().withMessage('Method is required'),
  body('headers').optional(),
  body('params').optional(),
  body('body').optional(),
  body('status').notEmpty().withMessage('Status is required'),
  body('response').optional(),
  body('duration').optional(),
  body('previousAttempts').optional().isArray(),
  validateRequest,
];

export const compareValidator = [
  body('attemptA').isObject().withMessage('Attempt A object is required'),
  body('attemptB').isObject().withMessage('Attempt B object is required'),
  validateRequest,
];

export const indexEpisodeValidator = [
  body('url').trim().notEmpty().withMessage('URL is required'),
  body('method').trim().notEmpty().withMessage('Method is required'),
  body('failedStatus').notEmpty().withMessage('Failed status is required'),
  body('errorSnippet').optional(),
  body('rootCauseLayer').optional(),
  body('appliedFix').optional(),
  body('successStatus').optional(),
  body('successDuration').optional(),
  validateRequest,
];

export const retrieveEpisodesValidator = [
  body('url').trim().notEmpty().withMessage('URL is required'),
  body('method').trim().notEmpty().withMessage('Method is required'),
  body('status').optional(),
  body('errorText').optional(),
  body('headersKeys').optional().isArray(),
  body('topK').optional().isNumeric(),
  validateRequest,
];
