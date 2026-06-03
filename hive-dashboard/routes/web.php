<?php

use Illuminate\Support\Facades\Route;
use App\Http\Controllers\Api\SyncController;

Route::get('/', function () {
    return view('welcome');
});

// API routes for hive sync (moved to routes/api.php)
// These are now at /api/sync/*

// Dashboard routes
Route::get('/dashboard', function () {
    return view('dashboard');
})->name('dashboard');

Route::get('/api/sync/facts', [SyncController::class, 'facts']);