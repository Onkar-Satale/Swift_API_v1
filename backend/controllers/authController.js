import jwt from 'jsonwebtoken';
import User from '../models/userModel.js';
import { ApiError } from '../utils/ApiError.js';

// JWT Token Generators
const generateAuthToken = (userId) => {
  return jwt.sign(
    { userId },
    process.env.JWT_ACCESS_SECRET,
    { expiresIn: process.env.JWT_ACCESS_EXPIRES_IN || '15m' }
  );
};

const generateRefreshToken = (userId) => {
  return jwt.sign(
    { userId },
    process.env.JWT_REFRESH_SECRET,
    { expiresIn: process.env.JWT_REFRESH_EXPIRES_IN || '30d' }
  );
};

// Cookie management helpers
const getCookieOptions = () => {
  const isProd = process.env.NODE_ENV === 'production';
  return {
    httpOnly: true,
    maxAge: 30 * 24 * 60 * 60 * 1000, // 30 days
    sameSite: isProd ? 'none' : 'lax',
    secure: isProd,
    path: '/'
  };
};

const setRefreshCookie = (res, token) => {
  res.cookie('refreshToken', token, getCookieOptions());
};

const clearRefreshCookie = (res) => {
  const isProd = process.env.NODE_ENV === 'production';
  res.clearCookie('refreshToken', {
    httpOnly: true,
    sameSite: isProd ? 'none' : 'lax',
    secure: isProd,
    path: '/'
  });
};

// Registers a new user account, creates JWT access & refresh tokens, and attaches refresh cookie.
export const register = async (req, res, next) => {
  try {
    const { email, password, firstName, lastName } = req.body;
    const lowercasedEmail = email ? email.toLowerCase() : email;
    
    const existingUser = await User.findOne({ email: lowercasedEmail });
    if (existingUser) {
      return next(new ApiError(409, 'User already exists with this email address. Please login or register with a different email.'));
    }

    const user = await User.create({ firstName, lastName, email: lowercasedEmail, password });
    
    const token = generateAuthToken(user._id);
    const refreshToken = generateRefreshToken(user._id);
    await User.findByIdAndUpdate(user._id, { refreshToken });
    
    setRefreshCookie(res, refreshToken);
    res.status(201).json({
      success: true,
      data: {
        token,
        email: user.email,
        firstName: user.firstName,
        lastName: user.lastName,
        createdAt: user.createdAt
      }
    });
  } catch (err) {
    next(err);
  }
};

// Authenticates user credentials, generates access & refresh tokens, and returns user metadata.
export const login = async (req, res, next) => {
  try {
    const { email, password } = req.body;
    const lowercasedEmail = email ? email.toLowerCase() : email;

    const user = await User.findOne({ email: lowercasedEmail }).select('+password');
    if (!user) return next(new ApiError(401, 'Invalid credentials'));

    const isMatch = await user.comparePassword(password);
    if (!isMatch) return next(new ApiError(401, 'Invalid credentials'));

    const token = generateAuthToken(user._id);
    const refreshToken = generateRefreshToken(user._id);
    await User.findByIdAndUpdate(user._id, { refreshToken });
    
    setRefreshCookie(res, refreshToken);

    res.json({
      success: true,
      data: {
        token,
        email: user.email,
        firstName: user.firstName,
        lastName: user.lastName,
        createdAt: user.createdAt
      }
    });
  } catch (err) {
    next(err);
  }
};

// Issues a new JWT access token using a valid HTTP-only refresh cookie.
export const refreshToken = async (req, res, next) => {
  try {
    const { refreshToken: token } = req.cookies;
    if (!token) {
      return next(new ApiError(401, 'No refresh token available'));
    }
    
    const decoded = jwt.verify(token, process.env.JWT_REFRESH_SECRET);
    const user = await User.findById(decoded.userId).select('+refreshToken');
    if (!user) {
      return next(new ApiError(401, 'User account no longer exists'));
    }
    
    const newAccessToken = generateAuthToken(user._id);
    const newRefreshToken = generateRefreshToken(user._id);
    await User.findByIdAndUpdate(user._id, { refreshToken: newRefreshToken });
    setRefreshCookie(res, newRefreshToken);

    res.json({ success: true, token: newAccessToken, userId: user._id });
  } catch (err) {
    return next(new ApiError(401, 'Refresh token expired or invalid', true, err.stack));
  }
};

// Logs out the user by invalidating the refresh token in the DB and clearing the refresh cookie.
export const logout = async (req, res, next) => {
  try {
    const { refreshToken: token } = req.cookies;
    if (token) {
      try {
        const decoded = jwt.verify(token, process.env.JWT_REFRESH_SECRET);
        await User.findByIdAndUpdate(decoded.userId, { $unset: { refreshToken: '' } });
      } catch (e) {
        // Silently swallow expiration errors during logout cleanup
      }
    }
    clearRefreshCookie(res);
    res.json({ success: true, message: 'Logged out successfully' });
  } catch (err) {
    next(err);
  }
};

// Permanently deletes the authenticated user's account and clears auth cookie.
export const deleteAccount = async (req, res, next) => {
  try {
    await User.findByIdAndDelete(req.userId);
    clearRefreshCookie(res);
    res.json({ success: true, message: 'Account deleted successfully' });
  } catch (err) {
    next(err);
  }
};
